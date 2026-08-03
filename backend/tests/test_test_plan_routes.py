"""Tests for backend/routes/test_plans.py — generate, list, feedback, edit,
approve, restart.

Rows are seeded directly via ``db_session`` (worker transitions are covered
in test_generate_test_plan.py); the queue is a recording stub.
"""

from types import SimpleNamespace

import pytest
from sqlmodel import select

from backend.config import MAX_TEST_PLAN_FEEDBACK_ROUNDS
from backend.models.database import (
    RequirementStatus,
    TestCase,
    TestEnvironmentStatus,
    TestPlan,
    TestPlanStatus,
)
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import _seed_test_case, _seed_test_env, _seed_test_plan


class _StubQueueService:
    """Records enqueued plan ids and returns fake jobs."""

    def __init__(self, available: bool = True):
        self.available = available
        self.enqueued_plans: list[int] = []

    def enqueue_test_plan(self, test_plan_id: int):
        if not self.available:
            return None
        self.enqueued_plans.append(test_plan_id)
        return SimpleNamespace(id=f"plan-job-{test_plan_id}")


@pytest.fixture
def stub_queue(monkeypatch):
    stub = _StubQueueService()
    import backend.routes.test_plans as test_plans_module

    monkeypatch.setattr(test_plans_module, "get_queue_service", lambda: stub)
    return stub


def _seed_locked_sprint(db_session, requirement_names=("Login", "Search"), active=True):
    """An active sprint with a confirmed test env + confirmed requirements."""
    sprint = _seed_sprint(db_session, active=active)
    _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)
    requirements = [
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED, name=name)
        for name in requirement_names
    ]
    return sprint, requirements


def _reload(db_session, plan_id) -> TestPlan:
    db_session.expire_all()
    return db_session.get(TestPlan, plan_id)


_VALID_EDIT = {
    "complexity": "high",
    "summary": "Edited summary.",
    "cases": [
        {
            "title": "Edited case",
            "preconditions": None,
            "steps": "Open the page\nDo the thing",
            "expected_result": "It works.",
            "case_type": "functional",
            "priority": "medium",
        }
    ],
}


def _edit_body(**overrides):
    body = {**_VALID_EDIT, "cases": [dict(_VALID_EDIT["cases"][0])]}
    case_overrides = overrides.pop("case", {})
    body.update(overrides)
    if body["cases"]:
        body["cases"][0].update(case_overrides)
    return body


# ── POST /api/sprints/{id}/test-plans/generate ───────────────────────


class TestGenerate:
    @pytest.mark.asyncio
    async def test_404_unknown_sprint(self, async_client, stub_queue):
        resp = await async_client.post("/api/sprints/99999/test-plans/generate")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_422_finished_sprint(self, async_client, db_session, stub_queue):
        sprint, _ = _seed_locked_sprint(db_session, active=False)

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/generate")

        assert resp.status_code == 422
        assert "finished" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_422_unless_environment_confirmed(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        # test env submitted but not confirmed → not locked
        _seed_test_env(db_session, sprint)

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/generate")

        assert resp.status_code == 422
        assert "Confirm the test environment" in resp.json()["detail"]
        assert stub_queue.enqueued_plans == []

    @pytest.mark.asyncio
    async def test_creates_pending_plan_per_requirement(self, async_client, db_session, stub_queue):
        sprint, requirements = _seed_locked_sprint(db_session)

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/generate")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert [row["requirement_id"] for row in data] == [r.id for r in requirements]
        assert [row["requirement_name"] for row in data] == ["Login", "Search"]
        assert all(row["status"] == "pending" for row in data)
        assert all(row["cases"] == [] for row in data)

    @pytest.mark.asyncio
    async def test_enqueues_and_persists_job_ids(self, async_client, db_session, stub_queue):
        sprint, _ = _seed_locked_sprint(db_session)

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/generate")

        ids = [row["id"] for row in resp.json()]
        assert stub_queue.enqueued_plans == ids
        for plan_id in ids:
            assert _reload(db_session, plan_id).job_id == f"plan-job-{plan_id}"

    @pytest.mark.asyncio
    async def test_redis_down_leaves_rows_pending(self, async_client, db_session, monkeypatch):
        stub = _StubQueueService(available=False)
        import backend.routes.test_plans as test_plans_module

        monkeypatch.setattr(test_plans_module, "get_queue_service", lambda: stub)
        sprint, _ = _seed_locked_sprint(db_session)

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/generate")

        assert resp.status_code == 200
        for row in resp.json():
            plan = _reload(db_session, row["id"])
            assert plan.status == TestPlanStatus.PENDING
            assert plan.job_id is None

    @pytest.mark.asyncio
    async def test_second_call_skips_existing_and_returns_all(
        self, async_client, db_session, stub_queue
    ):
        sprint, requirements = _seed_locked_sprint(db_session)
        existing = _seed_test_plan(
            db_session, requirements[0], status=TestPlanStatus.DRAFT, summary="Keep me"
        )

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/generate")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2  # full list, existing draft included
        by_req = {row["requirement_id"]: row for row in data}
        assert by_req[requirements[0].id]["id"] == existing.id
        assert by_req[requirements[0].id]["status"] == "draft"
        assert by_req[requirements[0].id]["summary"] == "Keep me"
        assert by_req[requirements[1].id]["status"] == "pending"
        # only the new row was enqueued
        assert stub_queue.enqueued_plans == [by_req[requirements[1].id]["id"]]

    @pytest.mark.asyncio
    async def test_resets_failed_plan(self, async_client, db_session, stub_queue):
        sprint, requirements = _seed_locked_sprint(db_session, requirement_names=("Login",))
        failed = _seed_test_plan(
            db_session,
            requirements[0],
            status=TestPlanStatus.FAILED,
            error="boom",
            retry_count=3,
            pending_feedback="resume this revision",
        )

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/generate")

        assert resp.status_code == 200
        row = _reload(db_session, failed.id)
        assert row.status == TestPlanStatus.PENDING
        assert row.error is None
        assert row.retry_count == 0
        assert row.pending_feedback == "resume this revision"
        assert stub_queue.enqueued_plans == [failed.id]


# ── GET /api/sprints/{id}/test-plans ─────────────────────────────────


class TestList:
    @pytest.mark.asyncio
    async def test_404_unknown_sprint(self, async_client):
        resp = await async_client.get("/api/sprints/99999/test-plans")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_empty_list_without_plans(self, async_client, db_session):
        sprint, _ = _seed_locked_sprint(db_session)
        resp = await async_client.get(f"/api/sprints/{sprint.id}/test-plans")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_lists_plans_with_ordered_cases(self, async_client, db_session):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(
            db_session,
            requirements[0],
            status=TestPlanStatus.DRAFT,
            complexity="medium",
            summary="Login plan",
        )
        _seed_test_case(db_session, plan, position=1, title="Second")
        _seed_test_case(db_session, plan, position=0, title="First")
        _seed_test_plan(db_session, requirements[1])

        resp = await async_client.get(f"/api/sprints/{sprint.id}/test-plans")

        assert resp.status_code == 200
        data = resp.json()
        assert [row["requirement_id"] for row in data] == [r.id for r in requirements]
        first = data[0]
        assert first["complexity"] == "medium"
        assert first["summary"] == "Login plan"
        assert first["requirement_name"] == "Login"
        assert first["requirement_description"] == "Users can log in."
        assert [case["title"] for case in first["cases"]] == ["First", "Second"]
        assert [case["position"] for case in first["cases"]] == [0, 1]

    @pytest.mark.asyncio
    async def test_readable_on_finished_sprint(self, async_client, db_session):
        sprint, requirements = _seed_locked_sprint(db_session, active=False)
        _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.APPROVED)

        resp = await async_client.get(f"/api/sprints/{sprint.id}/test-plans")

        assert resp.status_code == 200
        assert len(resp.json()) == 1


# ── POST /api/test-plans/{id}/feedback ───────────────────────────────


class TestFeedback:
    @pytest.mark.asyncio
    async def test_404_unknown_plan(self, async_client, stub_queue):
        resp = await async_client.post(
            "/api/test-plans/99999/feedback", json={"feedback": "More cases."}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_422_finished_sprint(self, async_client, db_session, stub_queue):
        sprint, requirements = _seed_locked_sprint(db_session, active=False)
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)

        resp = await async_client.post(
            f"/api/test-plans/{plan.id}/feedback", json={"feedback": "More cases."}
        )

        assert resp.status_code == 422
        assert "finished" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            TestPlanStatus.PENDING,
            TestPlanStatus.GENERATING,
            TestPlanStatus.APPROVED,
            TestPlanStatus.FAILED,
        ],
    )
    async def test_422_unless_draft(self, async_client, db_session, stub_queue, status):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(db_session, requirements[0], status=status)

        resp = await async_client.post(
            f"/api/test-plans/{plan.id}/feedback", json={"feedback": "More cases."}
        )

        assert resp.status_code == 422
        assert "Only draft plans can receive feedback." in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_past_feedback_cap(self, async_client, db_session, stub_queue):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(
            db_session,
            requirements[0],
            status=TestPlanStatus.DRAFT,
            revision_count=MAX_TEST_PLAN_FEEDBACK_ROUNDS,
        )

        resp = await async_client.post(
            f"/api/test-plans/{plan.id}/feedback", json={"feedback": "More cases."}
        )

        assert resp.status_code == 422
        assert "Feedback limit reached" in resp.json()["detail"]
        assert stub_queue.enqueued_plans == []

    @pytest.mark.asyncio
    async def test_422_empty_feedback(self, async_client, db_session, stub_queue):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)

        resp = await async_client.post(
            f"/api/test-plans/{plan.id}/feedback", json={"feedback": "   "}
        )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_sets_feedback_and_enqueues(self, async_client, db_session, stub_queue):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)

        resp = await async_client.post(
            f"/api/test-plans/{plan.id}/feedback", json={"feedback": "Add negative cases."}
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"
        row = _reload(db_session, plan.id)
        assert row.status == TestPlanStatus.PENDING
        assert row.pending_feedback == "Add negative cases."
        assert row.revision_count == 0  # bumped by the worker, not the route
        assert stub_queue.enqueued_plans == [plan.id]
        assert row.job_id == f"plan-job-{plan.id}"


# ── PATCH /api/test-plans/{id} ───────────────────────────────────────


class TestEdit:
    @pytest.mark.asyncio
    async def test_404_unknown_plan(self, async_client, stub_queue):
        resp = await async_client.patch("/api/test-plans/99999", json=_edit_body())
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_422_finished_sprint(self, async_client, db_session, stub_queue):
        sprint, requirements = _seed_locked_sprint(db_session, active=False)
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)

        resp = await async_client.patch(f"/api/test-plans/{plan.id}", json=_edit_body())

        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            TestPlanStatus.PENDING,
            TestPlanStatus.GENERATING,
            TestPlanStatus.APPROVED,
            TestPlanStatus.FAILED,
        ],
    )
    async def test_422_unless_draft(self, async_client, db_session, stub_queue, status):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(db_session, requirements[0], status=status)

        resp = await async_client.patch(f"/api/test-plans/{plan.id}", json=_edit_body())

        assert resp.status_code == 422
        assert "Only draft plans can be edited." in resp.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "field"),
        [
            (_edit_body(cases=[]), "case"),
            (_edit_body(case={"title": "   "}), "title"),
            (_edit_body(case={"steps": "  \n  "}), "step"),
            (_edit_body(case={"expected_result": ""}), "expected result"),
            (_edit_body(case={"case_type": " "}), "type"),
            (_edit_body(complexity="extreme"), "complexity"),
        ],
        ids=[
            "empty-cases",
            "blank-title",
            "blank-steps",
            "blank-expected-result",
            "blank-case-type",
            "bad-complexity",
        ],
    )
    async def test_422_validation_names_offending_field(
        self, async_client, db_session, stub_queue, body, field
    ):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)

        resp = await async_client.patch(f"/api/test-plans/{plan.id}", json=body)

        assert resp.status_code == 422
        assert field in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_422_invalid_priority_rejected_by_schema(
        self, async_client, db_session, stub_queue
    ):
        """priority is enum-typed on the request model — pydantic rejects it."""
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)

        resp = await async_client.patch(
            f"/api/test-plans/{plan.id}", json=_edit_body(case={"priority": "urgent"})
        )

        assert resp.status_code == 422
        assert "priority" in str(resp.json()["detail"]).lower()

    @pytest.mark.asyncio
    async def test_replaces_cases_and_stays_draft(self, async_client, db_session, stub_queue):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(
            db_session,
            requirements[0],
            status=TestPlanStatus.DRAFT,
            complexity="low",
            summary="Old",
            revision_count=1,
        )
        old_case = _seed_test_case(db_session, plan, position=0, title="Old case")
        old_case_id = old_case.id

        resp = await async_client.patch(
            f"/api/test-plans/{plan.id}",
            json=_edit_body(
                cases=[
                    dict(_VALID_EDIT["cases"][0], title="New A"),
                    dict(_VALID_EDIT["cases"][0], title="New B"),
                ]
            ),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "draft"
        assert data["complexity"] == "high"
        assert data["summary"] == "Edited summary."
        assert [case["title"] for case in data["cases"]] == ["New A", "New B"]
        assert [case["position"] for case in data["cases"]] == [0, 1]
        assert data["revision_count"] == 1  # direct edits never bump it

        # Archived, not deleted — the row stays for any run that used it,
        # but disappears from the plan's live case list.
        old_case_row = db_session.exec(select(TestCase).where(TestCase.id == old_case_id)).one()
        assert old_case_row.archived is True
        assert old_case_id not in {case["id"] for case in data["cases"]}
        assert stub_queue.enqueued_plans == []  # no LLM, no enqueue

    @pytest.mark.asyncio
    async def test_steps_normalized_on_save(self, async_client, db_session, stub_queue):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)

        resp = await async_client.patch(
            f"/api/test-plans/{plan.id}",
            json=_edit_body(case={"steps": "  First step  \n\n   \nSecond step\n"}),
        )

        assert resp.status_code == 200
        assert resp.json()["cases"][0]["steps"] == "First step\nSecond step"


# ── POST /api/test-plans/{id}/approve ────────────────────────────────


class TestApprove:
    @pytest.mark.asyncio
    async def test_404_unknown_plan(self, async_client):
        resp = await async_client.post("/api/test-plans/99999/approve")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_422_finished_sprint(self, async_client, db_session):
        sprint, requirements = _seed_locked_sprint(db_session, active=False)
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)

        resp = await async_client.post(f"/api/test-plans/{plan.id}/approve")

        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            TestPlanStatus.PENDING,
            TestPlanStatus.GENERATING,
            TestPlanStatus.APPROVED,
            TestPlanStatus.FAILED,
        ],
    )
    async def test_422_unless_draft(self, async_client, db_session, status):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(db_session, requirements[0], status=status)

        resp = await async_client.post(f"/api/test-plans/{plan.id}/approve")

        assert resp.status_code == 422
        assert "Only draft plans can be approved." in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_draft_to_approved_is_terminal(self, async_client, db_session, stub_queue):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)

        resp = await async_client.post(f"/api/test-plans/{plan.id}/approve")

        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"
        assert _reload(db_session, plan.id).status == TestPlanStatus.APPROVED

        # terminal: second approve, feedback, and edit all 422
        assert (await async_client.post(f"/api/test-plans/{plan.id}/approve")).status_code == 422
        assert (
            await async_client.post(
                f"/api/test-plans/{plan.id}/feedback", json={"feedback": "More."}
            )
        ).status_code == 422
        assert (
            await async_client.patch(f"/api/test-plans/{plan.id}", json=_edit_body())
        ).status_code == 422


# ── POST /api/sprints/{id}/test-plans/approve-all ────────────────────


class TestApproveAll:
    @pytest.mark.asyncio
    async def test_approves_draft_plans_only(self, async_client, db_session):
        sprint, requirements = _seed_locked_sprint(
            db_session, requirement_names=("A", "B", "C", "D")
        )
        draft = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)
        pending = _seed_test_plan(db_session, requirements[1], status=TestPlanStatus.PENDING)
        approved = _seed_test_plan(db_session, requirements[2], status=TestPlanStatus.APPROVED)
        failed = _seed_test_plan(db_session, requirements[3], status=TestPlanStatus.FAILED)

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/approve-all")

        assert resp.status_code == 200
        statuses = {row["id"]: row["status"] for row in resp.json()}
        assert statuses[draft.id] == "approved"
        assert statuses[pending.id] == "pending"
        assert statuses[approved.id] == "approved"
        assert statuses[failed.id] == "failed"

    @pytest.mark.asyncio
    async def test_returns_full_list_with_cases(self, async_client, db_session):
        sprint, requirements = _seed_locked_sprint(db_session, requirement_names=("Login",))
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)
        _seed_test_case(db_session, plan, position=0, title="Valid login")

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/approve-all")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["status"] == "approved"
        assert [case["title"] for case in data[0]["cases"]] == ["Valid login"]

    @pytest.mark.asyncio
    async def test_bumps_updated_at(self, async_client, db_session):
        sprint, requirements = _seed_locked_sprint(db_session, requirement_names=("Login",))
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)
        before = plan.updated_at

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/approve-all")

        assert resp.status_code == 200
        assert resp.json()[0]["updated_at"] >= before.isoformat()

    @pytest.mark.asyncio
    async def test_404_unknown_sprint(self, async_client):
        resp = await async_client.post("/api/sprints/99999/test-plans/approve-all")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_422_finished_sprint(self, async_client, db_session):
        sprint, requirements = _seed_locked_sprint(db_session, active=False)
        _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/approve-all")

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_noop_when_no_draft_plans(self, async_client, db_session):
        sprint, requirements = _seed_locked_sprint(db_session, requirement_names=("Login",))
        _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.PENDING)

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/approve-all")

        assert resp.status_code == 200
        assert resp.json()[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_scoped_to_sprint(self, async_client, db_session):
        sprint, _ = _seed_locked_sprint(db_session, requirement_names=("Login",))
        other_sprint, other_requirements = _seed_locked_sprint(
            db_session, requirement_names=("Other",)
        )
        other_plan = _seed_test_plan(db_session, other_requirements[0], status=TestPlanStatus.DRAFT)

        resp = await async_client.post(f"/api/sprints/{sprint.id}/test-plans/approve-all")

        assert resp.status_code == 200
        assert resp.json() == []
        assert _reload(db_session, other_plan.id).status == TestPlanStatus.DRAFT


# ── POST /api/test-plans/{id}/restart ────────────────────────────────


class TestRestart:
    @pytest.mark.asyncio
    async def test_404_unknown_plan(self, async_client, stub_queue):
        resp = await async_client.post("/api/test-plans/99999/restart")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_422_finished_sprint(self, async_client, db_session, stub_queue):
        sprint, requirements = _seed_locked_sprint(db_session, active=False)
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.FAILED)

        resp = await async_client.post(f"/api/test-plans/{plan.id}/restart")

        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            TestPlanStatus.PENDING,
            TestPlanStatus.GENERATING,
            TestPlanStatus.DRAFT,
            TestPlanStatus.APPROVED,
        ],
    )
    async def test_422_unless_failed(self, async_client, db_session, stub_queue, status):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(db_session, requirements[0], status=status)

        resp = await async_client.post(f"/api/test-plans/{plan.id}/restart")

        assert resp.status_code == 422
        assert "Only failed plans can be restarted." in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_restart_resets_and_enqueues(self, async_client, db_session, stub_queue):
        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(
            db_session,
            requirements[0],
            status=TestPlanStatus.FAILED,
            error="boom",
            retry_count=3,
            pending_feedback="resume this revision",
        )

        resp = await async_client.post(f"/api/test-plans/{plan.id}/restart")

        assert resp.status_code == 200
        row = _reload(db_session, plan.id)
        assert row.status == TestPlanStatus.PENDING
        assert row.error is None
        assert row.retry_count == 0
        assert row.pending_feedback == "resume this revision"
        assert stub_queue.enqueued_plans == [plan.id]
        assert row.job_id == f"plan-job-{plan.id}"


# ── Auth spot-check ──────────────────────────────────────────────────


class TestAuth:
    @pytest.mark.asyncio
    async def test_endpoints_401_without_cookie_when_password_set(
        self, async_client, db_session, monkeypatch
    ):
        import backend.config

        sprint, requirements = _seed_locked_sprint(db_session)
        plan = _seed_test_plan(db_session, requirements[0], status=TestPlanStatus.DRAFT)
        monkeypatch.setattr(backend.config, "APP_PASSWORD", "secret123")

        for method, url in [
            ("post", f"/api/sprints/{sprint.id}/test-plans/generate"),
            ("get", f"/api/sprints/{sprint.id}/test-plans"),
            ("post", f"/api/test-plans/{plan.id}/approve"),
        ]:
            resp = await getattr(async_client, method)(url)
            assert resp.status_code == 401
