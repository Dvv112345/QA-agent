"""Tests for backend/routes/test_execution.py — create runs, list, detail,
script download, restart.

Rows are seeded directly via ``db_session``; the queue is a recording stub.
"""

from types import SimpleNamespace

import pytest

from backend.models.database import (
    RequirementStatus,
    TestCaseExecutionStatus,
    TestExecution,
    TestExecutionStatus,
    TestPlanStatus,
    TestRun,
)
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import (
    _seed_test_case,
    _seed_test_case_execution,
    _seed_test_execution,
    _seed_test_plan,
    _seed_test_run,
)


class _StubQueueService:
    """Records enqueued execution ids and returns fake jobs."""

    def __init__(self, available: bool = True):
        self.available = available
        self.enqueued_executions: list[int] = []

    def enqueue_test_execution(self, test_execution_id: int):
        if not self.available:
            return None
        self.enqueued_executions.append(test_execution_id)
        return SimpleNamespace(id=f"execution-job-{test_execution_id}")


@pytest.fixture
def stub_queue(monkeypatch):
    stub = _StubQueueService()
    import backend.routes.test_execution as test_execution_module

    monkeypatch.setattr(test_execution_module, "get_queue_service", lambda: stub)
    return stub


def _seed_runnable_requirement(db_session, sprint, name="Login", case_count=2):
    """A confirmed requirement with an approved plan and cases, ready to run."""
    requirement = _seed_requirement(
        db_session, sprint, status=RequirementStatus.CONFIRMED, name=name
    )
    plan = _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED)
    for i in range(case_count):
        _seed_test_case(db_session, plan, position=i, title=f"{name} case {i}")
    db_session.refresh(requirement)
    return requirement


def _reload_run(db_session, run_id) -> TestRun:
    db_session.expire_all()
    return db_session.get(TestRun, run_id)


def _reload_execution(db_session, execution_id) -> TestExecution:
    db_session.expire_all()
    return db_session.get(TestExecution, execution_id)


# ── POST /api/sprints/{id}/test-runs ──────────────────────────────────


class TestCreateTestRun:
    @pytest.mark.asyncio
    async def test_404_unknown_sprint(self, async_client, stub_queue):
        resp = await async_client.post(
            "/api/sprints/99999/test-runs", json={"requirement_ids": [1]}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_422_finished_sprint(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session, active=False)
        requirement = _seed_runnable_requirement(db_session, sprint)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_422_empty_requirement_ids(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs", json={"requirement_ids": []}
        )
        assert resp.status_code == 422
        assert "at least one" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_422_foreign_or_unconfirmed_requirement(
        self, async_client, db_session, stub_queue
    ):
        sprint = _seed_sprint(db_session)
        not_confirmed = _seed_requirement(db_session, sprint, status=RequirementStatus.READY)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [not_confirmed.id, 99999]},
        )
        assert resp.status_code == 422
        assert str(not_confirmed.id) in resp.json()["detail"]
        assert "99999" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_plan_not_approved(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(
            db_session, sprint, status=RequirementStatus.CONFIRMED, name="No Plan"
        )

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )
        assert resp.status_code == 422
        assert "No Plan" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_plan_draft_not_approved(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(
            db_session, sprint, status=RequirementStatus.CONFIRMED, name="Draft Plan"
        )
        _seed_test_plan(db_session, requirement, status=TestPlanStatus.DRAFT)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )
        assert resp.status_code == 422
        assert "Draft Plan" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_already_in_progress(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint)
        run = _seed_test_run(db_session, sprint)
        _seed_test_execution(db_session, run, requirement, status=TestExecutionStatus.RUNNING)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )
        assert resp.status_code == 422
        assert requirement.name in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_creates_run_with_executions_and_cases_in_position_order(
        self, async_client, db_session, stub_queue
    ):
        sprint = _seed_sprint(db_session)
        login = _seed_runnable_requirement(db_session, sprint, name="Login", case_count=2)
        search = _seed_runnable_requirement(db_session, sprint, name="Search", case_count=1)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [login.id, search.id]},
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "running"
        assert len(data["executions"]) == 2
        assert data["executions"][0]["requirement_name"] == "Login"
        assert [c["test_case"]["title"] for c in data["executions"][0]["cases"]] == [
            "Login case 0",
            "Login case 1",
        ]
        assert data["executions"][1]["requirement_name"] == "Search"
        assert len(data["executions"][1]["cases"]) == 1
        assert all(c["status"] == "pending" for c in data["executions"][0]["cases"])

    @pytest.mark.asyncio
    async def test_enqueues_and_persists_job_id(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )

        assert resp.status_code == 201
        run_id = resp.json()["id"]
        execution_id = resp.json()["executions"][0]["id"]
        assert stub_queue.enqueued_executions == [execution_id]
        row = _reload_execution(db_session, execution_id)
        assert row.job_id == f"execution-job-{execution_id}"
        assert _reload_run(db_session, run_id) is not None

    @pytest.mark.asyncio
    async def test_redis_down_leaves_rows_pending_without_job_id(
        self, async_client, db_session, stub_queue
    ):
        stub_queue.available = False
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )

        assert resp.status_code == 201
        execution_id = resp.json()["executions"][0]["id"]
        row = _reload_execution(db_session, execution_id)
        assert row.status == TestExecutionStatus.PENDING
        assert row.job_id is None


# ── GET /api/sprints/{id}/test-runs ───────────────────────────────────


class TestListTestRuns:
    @pytest.mark.asyncio
    async def test_404_unknown_sprint(self, async_client):
        resp = await async_client.get("/api/sprints/99999/test-runs")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_newest_first(self, async_client, db_session):
        from datetime import datetime, timedelta, timezone

        sprint = _seed_sprint(db_session)
        older = _seed_test_run(
            db_session, sprint, created_at=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        newer = _seed_test_run(db_session, sprint, created_at=datetime.now(timezone.utc))

        resp = await async_client.get(f"/api/sprints/{sprint.id}/test-runs")

        assert resp.status_code == 200
        ids = [row["id"] for row in resp.json()]
        assert ids == [newer.id, older.id]

    @pytest.mark.asyncio
    async def test_counts_across_mixed_case_statuses(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=3)
        plan = requirement.test_plan
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(
            db_session, run, requirement, status=TestExecutionStatus.COMPLETED
        )
        statuses = [
            TestCaseExecutionStatus.PASSED,
            TestCaseExecutionStatus.FAILED,
            TestCaseExecutionStatus.ERROR,
        ]
        for case, status in zip(plan.cases, statuses, strict=True):
            _seed_test_case_execution(db_session, execution, case, status=status)

        resp = await async_client.get(f"/api/sprints/{sprint.id}/test-runs")

        assert resp.status_code == 200
        row = resp.json()[0]
        assert row["total_cases"] == 3
        assert row["passed_cases"] == 1
        assert row["failed_cases"] == 1
        assert row["error_cases"] == 1
        assert row["status"] == "completed"
        assert row["requirement_names"] == [requirement.name]


# ── GET /api/test-runs/{id} ───────────────────────────────────────────


class TestGetTestRun:
    @pytest.mark.asyncio
    async def test_404_unknown_run(self, async_client):
        resp = await async_client.get("/api/test-runs/99999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_nested_structure(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        plan = requirement.test_plan
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(db_session, run, requirement)
        _seed_test_case_execution(db_session, execution, plan.cases[0])

        resp = await async_client.get(f"/api/test-runs/{run.id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == run.id
        assert data["sprint_id"] == sprint.id
        assert len(data["executions"]) == 1
        assert data["executions"][0]["requirement_id"] == requirement.id
        assert len(data["executions"][0]["cases"]) == 1
        assert data["executions"][0]["cases"][0]["test_case"]["title"] == plan.cases[0].title


# ── GET /api/test-case-executions/{id}/script ─────────────────────────


class TestDownloadScript:
    @pytest.mark.asyncio
    async def test_200_with_content_and_headers(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        plan = requirement.test_plan
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(db_session, run, requirement)
        case_execution = _seed_test_case_execution(
            db_session,
            execution,
            plan.cases[0],
            status=TestCaseExecutionStatus.PASSED,
            script_snapshot="print('hello')\n",
        )

        resp = await async_client.get(f"/api/test-case-executions/{case_execution.id}/script")

        assert resp.status_code == 200
        assert resp.text == "print('hello')\n"
        assert "attachment" in resp.headers["content-disposition"]
        assert f"test_case_{case_execution.id}.py" in resp.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_404_when_not_yet_run(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        plan = requirement.test_plan
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(db_session, run, requirement)
        case_execution = _seed_test_case_execution(db_session, execution, plan.cases[0])

        resp = await async_client.get(f"/api/test-case-executions/{case_execution.id}/script")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_404_for_missing_row(self, async_client):
        resp = await async_client.get("/api/test-case-executions/99999/script")
        assert resp.status_code == 404


# ── POST /api/test-executions/{id}/restart ────────────────────────────


class TestRestartTestExecution:
    @pytest.mark.asyncio
    async def test_404_for_missing_row(self, async_client):
        resp = await async_client.post("/api/test-executions/99999/restart")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_only_from_failed(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(
            db_session, run, requirement, status=TestExecutionStatus.COMPLETED
        )

        resp = await async_client.post(f"/api/test-executions/{execution.id}/restart")

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_clears_error_and_retries_then_enqueues(
        self, async_client, db_session, stub_queue
    ):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(
            db_session,
            run,
            requirement,
            status=TestExecutionStatus.FAILED,
            error="boom",
            retry_count=3,
        )

        resp = await async_client.post(f"/api/test-executions/{execution.id}/restart")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["error"] is None
        assert stub_queue.enqueued_executions == [execution.id]
        row = _reload_execution(db_session, execution.id)
        assert row.retry_count == 0
        assert row.job_id == f"execution-job-{execution.id}"

    @pytest.mark.asyncio
    async def test_422_on_finished_sprint(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session, active=False)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(
            db_session, run, requirement, status=TestExecutionStatus.FAILED
        )

        resp = await async_client.post(f"/api/test-executions/{execution.id}/restart")

        assert resp.status_code == 422


# ── Auth spot-check ────────────────────────────────────────────────────


class TestAuth:
    @pytest.mark.asyncio
    async def test_401_without_cookie(self, monkeypatch, db_session):
        from backend.tests.test_auth_routes import _make_client

        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.get("/api/sprints/1/test-runs")
        assert resp.status_code == 401
