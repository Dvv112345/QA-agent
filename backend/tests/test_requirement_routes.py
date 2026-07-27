"""Tests for backend/routes/requirements.py — requirement CRUD and state transitions."""

import uuid
from types import SimpleNamespace

import pytest

from backend.models.database import Repo, Requirement, RequirementStatus, Sprint

# ── Seeding helpers ───────────────────────────────────────────────────
# Requirements are seeded directly through the DB session because the API
# alone can't reach intermediate statuses (needs_clarification, failed, …)
# without a running worker.


def _seed_sprint(db_session, active: bool = True, readme_user_provided: bool = False) -> Sprint:
    repo = Repo(github_link="https://github.com/owner/repo", name="owner/repo")
    db_session.add(repo)
    db_session.commit()
    sprint = Sprint(
        name="Sprint",
        repo_id=repo.id,
        active=active,
        directory=f"dir-{uuid.uuid4().hex[:12]}",
        readme_user_provided=readme_user_provided,
    )
    db_session.add(sprint)
    db_session.commit()
    db_session.refresh(sprint)
    return sprint


def _seed_requirement(
    db_session,
    sprint: Sprint,
    status: RequirementStatus = RequirementStatus.PENDING,
    **kwargs,
) -> Requirement:
    requirement = Requirement(
        sprint_id=sprint.id,
        name=kwargs.pop("name", "Login"),
        description=kwargs.pop("description", "Users can log in."),
        original_description=kwargs.pop("original_description", "Users can log in."),
        status=status,
        **kwargs,
    )
    db_session.add(requirement)
    db_session.commit()
    db_session.refresh(requirement)
    return requirement


# ── POST /api/sprints/{id}/requirements ──────────────────────────────


class TestCreateRequirements:
    @pytest.mark.asyncio
    async def test_batch_create(self, async_client, db_session):
        sprint = _seed_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/requirements",
            json=[
                {"name": "Login", "description": "Users can log in."},
                {"name": "Logout", "description": "Users can log out."},
            ],
        )

        assert resp.status_code == 201
        data = resp.json()
        assert len(data) == 2
        for row in data:
            assert row["sprint_id"] == sprint.id
            assert row["status"] == "pending"
            assert row["revision_count"] == 0
            assert row["clarifying_question"] is None
            assert row["error"] is None
            assert row["original_description"] == row["description"]
        assert data[0]["name"] == "Login"
        assert data[1]["name"] == "Logout"

    @pytest.mark.asyncio
    async def test_strips_whitespace(self, async_client, db_session):
        sprint = _seed_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/requirements",
            json=[{"name": "  Login  ", "description": "  Users log in.  "}],
        )

        assert resp.status_code == 201
        assert resp.json()[0]["name"] == "Login"
        assert resp.json()[0]["description"] == "Users log in."

    @pytest.mark.asyncio
    async def test_empty_list_rejected(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        resp = await async_client.post(f"/api/sprints/{sprint.id}/requirements", json=[])
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_blank_fields_rejected(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        for body in (
            [{"name": "   ", "description": "desc"}],
            [{"name": "name", "description": "   "}],
        ):
            resp = await async_client.post(f"/api/sprints/{sprint.id}/requirements", json=body)
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_sprint_404(self, async_client, db_session):
        resp = await async_client.post(
            "/api/sprints/99999/requirements",
            json=[{"name": "Login", "description": "desc"}],
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_inactive_sprint_422(self, async_client, db_session):
        sprint = _seed_sprint(db_session, active=False)
        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/requirements",
            json=[{"name": "Login", "description": "desc"}],
        )
        assert resp.status_code == 422


# ── GET /api/sprints/{id}/requirements ───────────────────────────────


class TestListRequirements:
    @pytest.mark.asyncio
    async def test_lists_in_creation_order(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        first = _seed_requirement(db_session, sprint, name="First")
        second = _seed_requirement(db_session, sprint, name="Second")

        resp = await async_client.get(f"/api/sprints/{sprint.id}/requirements")

        assert resp.status_code == 200
        ids = [row["id"] for row in resp.json()]
        assert ids == [first.id, second.id]

    @pytest.mark.asyncio
    async def test_empty(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        resp = await async_client.get(f"/api/sprints/{sprint.id}/requirements")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_unknown_sprint_404(self, async_client, db_session):
        resp = await async_client.get("/api/sprints/99999/requirements")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_internal_fields_not_exposed(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.NEEDS_CLARIFICATION,
            clarifying_question="Which users?",
            pending_answer="All users",
            job_id="job-123",
        )

        resp = await async_client.get(f"/api/sprints/{sprint.id}/requirements")

        row = resp.json()[0]
        assert "pending_answer" not in row
        assert "job_id" not in row
        assert "retry_count" not in row
        assert "last_heartbeat" not in row

    @pytest.mark.asyncio
    async def test_clarification_cap_flag(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        below_cap = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.NEEDS_CLARIFICATION,
            clarifying_question="Which users?",
            revision_count=2,
        )
        at_cap = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.NEEDS_CLARIFICATION,
            clarifying_question="Which users?",
            revision_count=3,
        )

        resp = await async_client.get(f"/api/sprints/{sprint.id}/requirements")

        rows = {row["id"]: row for row in resp.json()}
        assert rows[below_cap.id]["clarification_cap_reached"] is False
        assert rows[at_cap.id]["clarification_cap_reached"] is True


# ── POST /api/requirements/{id}/answer ───────────────────────────────


class TestAnswerRequirement:
    @pytest.mark.asyncio
    async def test_answer_from_needs_clarification(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.NEEDS_CLARIFICATION,
            clarifying_question="Which users?",
        )

        resp = await async_client.post(
            f"/api/requirements/{req.id}/answer",
            json={"answer": "All registered users."},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"
        db_session.refresh(req)
        assert req.pending_answer == "All registered users."

    @pytest.mark.asyncio
    async def test_wrong_status_422(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=RequirementStatus.READY)

        resp = await async_client.post(f"/api/requirements/{req.id}/answer", json={"answer": "hi"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_cap_reached_422(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.NEEDS_CLARIFICATION,
            clarifying_question="Q?",
            revision_count=3,
        )

        resp = await async_client.post(f"/api/requirements/{req.id}/answer", json={"answer": "hi"})
        assert resp.status_code == 422
        assert "Clarification limit reached" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_blank_answer_422(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.NEEDS_CLARIFICATION,
            clarifying_question="Q?",
        )

        resp = await async_client.post(f"/api/requirements/{req.id}/answer", json={"answer": "   "})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_404(self, async_client, db_session):
        resp = await async_client.post("/api/requirements/99999/answer", json={"answer": "hi"})
        assert resp.status_code == 404


# ── POST /api/requirements/{id}/confirm ──────────────────────────────


class TestConfirmRequirement:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [RequirementStatus.NEEDS_CLARIFICATION, RequirementStatus.READY],
    )
    async def test_confirm(self, async_client, db_session, status):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=status)

        resp = await async_client.post(f"/api/requirements/{req.id}/confirm")

        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [RequirementStatus.PENDING, RequirementStatus.ANALYZING, RequirementStatus.FAILED],
    )
    async def test_wrong_status_422(self, async_client, db_session, status):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=status)

        resp = await async_client.post(f"/api/requirements/{req.id}/confirm")
        assert resp.status_code == 422


# ── POST /api/sprints/{id}/requirements/confirm-all ──────────────────


class TestConfirmAll:
    @pytest.mark.asyncio
    async def test_confirms_ready_and_needs_clarification_only(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        ready = _seed_requirement(db_session, sprint, status=RequirementStatus.READY, name="A")
        needs_clarification = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.NEEDS_CLARIFICATION,
            name="B",
            clarifying_question="Which users?",
        )
        pending = _seed_requirement(db_session, sprint, status=RequirementStatus.PENDING, name="C")
        analyzing = _seed_requirement(
            db_session, sprint, status=RequirementStatus.ANALYZING, name="D"
        )
        confirmed = _seed_requirement(
            db_session, sprint, status=RequirementStatus.CONFIRMED, name="E"
        )
        failed = _seed_requirement(
            db_session, sprint, status=RequirementStatus.FAILED, name="F", error="boom"
        )

        resp = await async_client.post(f"/api/sprints/{sprint.id}/requirements/confirm-all")

        assert resp.status_code == 200
        statuses = {row["id"]: row["status"] for row in resp.json()}
        assert statuses[ready.id] == "confirmed"
        assert statuses[needs_clarification.id] == "confirmed"
        assert statuses[pending.id] == "pending"
        assert statuses[analyzing.id] == "analyzing"
        assert statuses[confirmed.id] == "confirmed"
        assert statuses[failed.id] == "failed"

    @pytest.mark.asyncio
    async def test_returns_full_sprint_list_in_creation_order(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        first = _seed_requirement(db_session, sprint, status=RequirementStatus.READY, name="A")
        second = _seed_requirement(db_session, sprint, status=RequirementStatus.PENDING, name="B")

        resp = await async_client.post(f"/api/sprints/{sprint.id}/requirements/confirm-all")

        assert resp.status_code == 200
        ids = [row["id"] for row in resp.json()]
        assert ids == [first.id, second.id]

    @pytest.mark.asyncio
    async def test_bumps_updated_at(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=RequirementStatus.READY)
        before = req.updated_at

        resp = await async_client.post(f"/api/sprints/{sprint.id}/requirements/confirm-all")

        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["id"] == req.id)
        assert row["updated_at"] >= before.isoformat()

    @pytest.mark.asyncio
    async def test_unknown_sprint_404(self, async_client, db_session):
        resp = await async_client.post("/api/sprints/99999/requirements/confirm-all")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_inactive_sprint_422(self, async_client, db_session):
        sprint = _seed_sprint(db_session, active=False)
        resp = await async_client.post(f"/api/sprints/{sprint.id}/requirements/confirm-all")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_noop_when_nothing_eligible(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.PENDING)

        resp = await async_client.post(f"/api/sprints/{sprint.id}/requirements/confirm-all")

        assert resp.status_code == 200
        assert resp.json()[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_scoped_to_sprint(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        other_sprint = _seed_sprint(db_session)
        other_req = _seed_requirement(
            db_session, other_sprint, status=RequirementStatus.READY, name="Other"
        )

        resp = await async_client.post(f"/api/sprints/{sprint.id}/requirements/confirm-all")

        assert resp.status_code == 200
        assert resp.json() == []
        db_session.refresh(other_req)
        assert other_req.status == RequirementStatus.READY


# ── PATCH /api/requirements/{id} ─────────────────────────────────────


class TestEditRequirement:
    @pytest.mark.asyncio
    async def test_edit_from_ready(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=RequirementStatus.READY)

        resp = await async_client.patch(
            f"/api/requirements/{req.id}",
            json={"description": "New description."},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["description"] == "New description."
        assert data["clarifying_question"] is None

    @pytest.mark.asyncio
    async def test_edit_from_needs_clarification(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.NEEDS_CLARIFICATION,
            clarifying_question="Which users?",
            pending_answer="stale",
        )

        resp = await async_client.patch(
            f"/api/requirements/{req.id}",
            json={"description": "New description."},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["clarifying_question"] is None
        db_session.refresh(req)
        assert req.pending_answer is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [RequirementStatus.PENDING, RequirementStatus.ANALYZING, RequirementStatus.FAILED],
    )
    async def test_wrong_status_422(self, async_client, db_session, status):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=status)

        resp = await async_client.patch(f"/api/requirements/{req.id}", json={"description": "New."})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_blank_description_422(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=RequirementStatus.READY)

        resp = await async_client.patch(f"/api/requirements/{req.id}", json={"description": "   "})
        assert resp.status_code == 422


# ── POST /api/requirements/{id}/restart ──────────────────────────────


class TestRestartRequirement:
    @pytest.mark.asyncio
    async def test_restart_from_failed(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.FAILED,
            error="LLM exploded",
            retry_count=3,
        )

        resp = await async_client.post(f"/api/requirements/{req.id}/restart")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["error"] is None
        db_session.refresh(req)
        assert req.retry_count == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [RequirementStatus.PENDING, RequirementStatus.READY, RequirementStatus.NEEDS_CLARIFICATION],
    )
    async def test_wrong_status_422(self, async_client, db_session, status):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=status)

        resp = await async_client.post(f"/api/requirements/{req.id}/restart")
        assert resp.status_code == 422


# ── Confirmed is content-terminal ────────────────────────────────────


class TestConfirmedTerminal:
    @pytest.mark.asyncio
    async def test_all_mutations_rejected(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)

        answer = await async_client.post(
            f"/api/requirements/{req.id}/answer", json={"answer": "hi"}
        )
        confirm = await async_client.post(f"/api/requirements/{req.id}/confirm")
        edit = await async_client.patch(f"/api/requirements/{req.id}", json={"description": "New."})
        restart = await async_client.post(f"/api/requirements/{req.id}/restart")

        assert answer.status_code == 422
        assert confirm.status_code == 422
        assert edit.status_code == 422
        assert restart.status_code == 422


# ── DELETE /api/requirements/{id} ────────────────────────────────────


class TestDeleteRequirement:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            RequirementStatus.PENDING,
            RequirementStatus.ANALYZING,
            RequirementStatus.NEEDS_CLARIFICATION,
            RequirementStatus.READY,
            RequirementStatus.CONFIRMED,
            RequirementStatus.FAILED,
        ],
    )
    async def test_delete_from_every_status(self, async_client, db_session, status):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=status)

        resp = await async_client.delete(f"/api/requirements/{req.id}")
        assert resp.status_code == 204

        listing = await async_client.get(f"/api/sprints/{sprint.id}/requirements")
        assert listing.json() == []

    @pytest.mark.asyncio
    async def test_unknown_404(self, async_client, db_session):
        resp = await async_client.delete("/api/requirements/99999")
        assert resp.status_code == 404


# ── Finished sprint blocks all mutations ─────────────────────────────


class TestFinishedSprint:
    @pytest.mark.asyncio
    async def test_mutations_blocked(self, async_client, db_session):
        sprint = _seed_sprint(db_session, active=False)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.NEEDS_CLARIFICATION,
            clarifying_question="Q?",
        )

        answer = await async_client.post(
            f"/api/requirements/{req.id}/answer", json={"answer": "hi"}
        )
        confirm = await async_client.post(f"/api/requirements/{req.id}/confirm")
        edit = await async_client.patch(f"/api/requirements/{req.id}", json={"description": "New."})
        restart = await async_client.post(f"/api/requirements/{req.id}/restart")
        delete = await async_client.delete(f"/api/requirements/{req.id}")

        assert answer.status_code == 422
        assert confirm.status_code == 422
        assert edit.status_code == 422
        assert restart.status_code == 422
        assert delete.status_code == 422

    @pytest.mark.asyncio
    async def test_listing_still_works(self, async_client, db_session):
        sprint = _seed_sprint(db_session, active=False)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)

        resp = await async_client.get(f"/api/sprints/{sprint.id}/requirements")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


# ── Enqueue wiring (recording stub) ──────────────────────────────────


class _StubQueueService:
    """Records enqueued requirement ids and returns fake jobs."""

    def __init__(self, available: bool = True):
        self.available = available
        self.enqueued: list[int] = []

    def enqueue_analysis(self, requirement_id: int):
        if not self.available:
            return None
        self.enqueued.append(requirement_id)
        return SimpleNamespace(id=f"job-{requirement_id}")


@pytest.fixture
def stub_queue(monkeypatch):
    stub = _StubQueueService()
    import backend.routes.requirements as requirements_module

    monkeypatch.setattr(requirements_module, "get_queue_service", lambda: stub)
    return stub


class TestEnqueueWiring:
    @pytest.mark.asyncio
    async def test_create_enqueues_each_row(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/requirements",
            json=[
                {"name": "A", "description": "a"},
                {"name": "B", "description": "b"},
            ],
        )

        ids = [row["id"] for row in resp.json()]
        assert stub_queue.enqueued == ids
        for req_id in ids:
            row = db_session.get(Requirement, req_id)
            db_session.refresh(row)
            assert row.job_id == f"job-{req_id}"

    @pytest.mark.asyncio
    async def test_answer_enqueues(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.NEEDS_CLARIFICATION,
            clarifying_question="Q?",
        )

        await async_client.post(f"/api/requirements/{req.id}/answer", json={"answer": "A."})
        assert stub_queue.enqueued == [req.id]

    @pytest.mark.asyncio
    async def test_edit_enqueues(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=RequirementStatus.READY)

        await async_client.patch(f"/api/requirements/{req.id}", json={"description": "New."})
        assert stub_queue.enqueued == [req.id]

    @pytest.mark.asyncio
    async def test_restart_enqueues(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=RequirementStatus.FAILED)

        await async_client.post(f"/api/requirements/{req.id}/restart")
        assert stub_queue.enqueued == [req.id]

    @pytest.mark.asyncio
    async def test_confirm_and_delete_do_not_enqueue(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        ready = _seed_requirement(db_session, sprint, status=RequirementStatus.READY)
        pending = _seed_requirement(db_session, sprint, status=RequirementStatus.PENDING)

        await async_client.post(f"/api/requirements/{ready.id}/confirm")
        await async_client.delete(f"/api/requirements/{pending.id}")
        assert stub_queue.enqueued == []

    @pytest.mark.asyncio
    async def test_enqueue_failure_leaves_row_pending(self, async_client, db_session, monkeypatch):
        stub = _StubQueueService(available=False)
        import backend.routes.requirements as requirements_module

        monkeypatch.setattr(requirements_module, "get_queue_service", lambda: stub)
        sprint = _seed_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/requirements",
            json=[{"name": "A", "description": "a"}],
        )

        assert resp.status_code == 201
        row = db_session.get(Requirement, resp.json()[0]["id"])
        db_session.refresh(row)
        assert row.status == RequirementStatus.PENDING
        assert row.job_id is None


# == Requirement lock (test environment confirmed) ====================


class TestRequirementsLock:
    """Once the test environment is confirmed, the requirement set is frozen."""

    def _seed_locked_sprint(self, db_session):
        from backend.models.database import TestEnvironmentStatus
        from backend.tests.test_sprints import _seed_test_env

        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)
        return sprint

    @pytest.mark.asyncio
    async def test_create_422_when_locked(self, async_client, db_session):
        sprint = self._seed_locked_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/requirements",
            json=[{"name": "New", "description": "New requirement."}],
        )

        assert resp.status_code == 422
        assert "locked" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_delete_422_when_locked(self, async_client, db_session):
        sprint = self._seed_locked_sprint(db_session)
        confirmed = sprint.requirements[0]

        resp = await async_client.delete(f"/api/requirements/{confirmed.id}")

        assert resp.status_code == 422
        assert "locked" in resp.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("te_status", ["needs_info", "ready"])
    async def test_create_and_delete_allowed_before_lock(self, async_client, db_session, te_status):
        from backend.tests.test_sprints import _seed_test_env

        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        _seed_test_env(db_session, sprint, status=te_status)
        doomed = _seed_requirement(db_session, sprint, name="Doomed")

        create_resp = await async_client.post(
            f"/api/sprints/{sprint.id}/requirements",
            json=[{"name": "New", "description": "New requirement."}],
        )
        delete_resp = await async_client.delete(f"/api/requirements/{doomed.id}")

        assert create_resp.status_code == 201
        assert delete_resp.status_code == 204

    @pytest.mark.asyncio
    async def test_other_transitions_unaffected_by_lock(self, async_client, db_session):
        """answer/confirm/edit/restart have no lock check (content-terminal rows only)."""
        sprint = self._seed_locked_sprint(db_session)
        needs = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.NEEDS_CLARIFICATION,
            name="Needs",
            clarifying_question="Which users?",
        )
        ready = _seed_requirement(db_session, sprint, status=RequirementStatus.READY, name="Ready")
        ready2 = _seed_requirement(
            db_session, sprint, status=RequirementStatus.READY, name="Ready2"
        )
        failed = _seed_requirement(
            db_session, sprint, status=RequirementStatus.FAILED, name="Failed"
        )

        answer_resp = await async_client.post(
            f"/api/requirements/{needs.id}/answer", json={"answer": "Registered users."}
        )
        edit_resp = await async_client.patch(
            f"/api/requirements/{ready.id}", json={"description": "Edited."}
        )
        confirm_resp = await async_client.post(f"/api/requirements/{ready2.id}/confirm")
        restart_resp = await async_client.post(f"/api/requirements/{failed.id}/restart")

        assert answer_resp.status_code == 200
        assert edit_resp.status_code == 200
        assert confirm_resp.status_code == 200
        assert restart_resp.status_code == 200
