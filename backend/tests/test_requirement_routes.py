"""Tests for backend/routes/requirements.py — requirement CRUD and state transitions."""

import uuid

import pytest

from backend.models.database import Repo, Requirement, RequirementStatus, Sprint

# ── Seeding helpers ───────────────────────────────────────────────────
# Requirements are seeded directly through the DB session because the API
# alone can't reach intermediate statuses (needs_clarification, failed, …)
# without a running worker.


def _seed_sprint(db_session, active: bool = True) -> Sprint:
    repo = Repo(github_link="https://github.com/owner/repo", name="owner/repo")
    db_session.add(repo)
    db_session.commit()
    sprint = Sprint(
        name="Sprint",
        repo_id=repo.id,
        active=active,
        directory=f"dir-{uuid.uuid4().hex[:12]}",
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
