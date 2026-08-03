"""Tests for backend/routes/sprints.py — sprint CRUD and README resolution."""

import base64

import pytest

from backend.tests.conftest import _create_repo  # noqa: F401 — used by tests

# ── POST /api/sprints ────────────────────────────────────────────────


class TestCreateSprint:
    """Tests for ``POST /api/sprints``."""

    @pytest.mark.asyncio
    async def test_creates_sprint_with_github_readme(self, async_client, httpx_mock, db_session):
        readme_content = "# Test README"
        encoded = base64.b64encode(readme_content.encode()).decode()

        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)

        # Metadata refresh during sprint creation
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo",
            json={"full_name": "owner/test-repo", "description": "Updated desc"},
        )
        # download_readme (returns content)
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo/readme",
            json={"content": encoded},
        )

        resp = await async_client.post(
            "/api/sprints",
            data={"name": "Sprint 1", "repo_id": str(repo_id)},
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Sprint 1"
        assert data["repo_id"] == repo_id
        assert data["active"] is True
        assert "directory" in data
        assert data["repo"] is not None
        assert data["repo"]["name"] == "owner/test-repo"

        from backend.models.database import Sprint

        db_sprint = db_session.get(Sprint, data["id"])
        assert db_sprint.readme_user_provided is False

    @pytest.mark.asyncio
    async def test_creates_sprint_with_user_readme(self, async_client, httpx_mock, db_session):
        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)

        # Metadata refresh during sprint creation
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo",
            json={"full_name": "owner/test-repo", "description": "Updated desc"},
        )

        resp = await async_client.post(
            "/api/sprints",
            data={"name": "Sprint 2", "repo_id": str(repo_id)},
            files={"readme_file": ("README.md", b"# Custom README", "text/markdown")},
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Sprint 2"
        assert data["active"] is True

        from backend.models.database import Sprint

        db_sprint = db_session.get(Sprint, data["id"])
        assert db_sprint.readme_user_provided is True

    @pytest.mark.asyncio
    async def test_rejects_readme_over_upload_size_cap(self, async_client, httpx_mock, monkeypatch):
        import backend.routes.sprints as sprints_module

        monkeypatch.setattr(sprints_module, "MAX_UPLOAD_SIZE_MB", 0)
        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)

        # Metadata refresh during sprint creation
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo",
            json={"full_name": "owner/test-repo", "description": "Updated desc"},
        )

        resp = await async_client.post(
            "/api/sprints",
            data={"name": "Sprint 2", "repo_id": str(repo_id)},
            files={"readme_file": ("README.md", b"# Custom README", "text/markdown")},
        )

        assert resp.status_code == 422
        assert "upload limit" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_requires_readme_when_github_has_none(self, async_client, httpx_mock):
        repo_id = await _create_repo(async_client, "https://github.com/owner/no-readme", httpx_mock)

        # Metadata refresh
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/no-readme",
            json={"full_name": "owner/no-readme", "description": "No README here"},
        )
        # download_readme returns None (404)
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/no-readme/readme",
            status_code=404,
        )

        resp = await async_client.post(
            "/api/sprints",
            data={"name": "Sprint 3", "repo_id": str(repo_id)},
        )

        assert resp.status_code == 422
        assert "does not have a README" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_empty_name(self, async_client, httpx_mock):
        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)

        resp = await async_client.post(
            "/api/sprints",
            data={"name": "   ", "repo_id": str(repo_id)},
        )

        assert resp.status_code == 422
        assert "name is required" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_rejects_nonexistent_repo(self, async_client):
        resp = await async_client.post(
            "/api/sprints",
            data={"name": "Sprint", "repo_id": "99999"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rejects_deactivated_repo(self, async_client, httpx_mock):
        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)
        await async_client.post(f"/api/repos/{repo_id}/deactivate")

        resp = await async_client.post(
            "/api/sprints",
            data={"name": "Sprint", "repo_id": str(repo_id)},
        )

        assert resp.status_code == 422
        assert "deactivated" in resp.json()["detail"].lower()


# ── GET /api/sprints ─────────────────────────────────────────────────


class TestListSprints:
    """Tests for ``GET /api/sprints``."""

    @pytest.mark.asyncio
    async def test_lists_sprints_with_repos(self, async_client, httpx_mock):
        readme_content = "# README"
        encoded = base64.b64encode(readme_content.encode()).decode()

        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)

        # Metadata refresh
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo",
            json={"full_name": "owner/test-repo", "description": "desc"},
        )
        # download_readme returns content
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo/readme",
            json={"content": encoded},
        )
        await async_client.post(
            "/api/sprints",
            data={"name": "Sprint A", "repo_id": str(repo_id)},
        )

        resp = await async_client.get("/api/sprints")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Sprint A"
        assert data[0]["repo"]["name"] == "owner/test-repo"

    @pytest.mark.asyncio
    async def test_lists_active_first(self, async_client, httpx_mock):
        readme_content = "# README"
        encoded = base64.b64encode(readme_content.encode()).decode()

        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)

        for i in range(2):
            # Metadata refresh
            httpx_mock.add_response(
                url="https://api.github.com/repos/owner/test-repo",
                json={"full_name": "owner/test-repo", "description": "desc"},
            )
            # download_readme returns content
            httpx_mock.add_response(
                url="https://api.github.com/repos/owner/test-repo/readme",
                json={"content": encoded},
            )
            resp = await async_client.post(
                "/api/sprints",
                data={"name": f"Sprint {i + 1}", "repo_id": str(repo_id)},
            )
            if i == 1:
                sprint_id = resp.json()["id"]
                await async_client.patch(
                    f"/api/sprints/{sprint_id}",
                    json={"active": False},
                )

        resp = await async_client.get("/api/sprints")

        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["active"] is True
        assert data[0]["name"] == "Sprint 1"
        assert data[1]["active"] is False

    @pytest.mark.asyncio
    async def test_returns_empty_list(self, async_client):
        resp = await async_client.get("/api/sprints")
        assert resp.status_code == 200
        assert resp.json() == []


# ── GET /api/sprints/{id} ────────────────────────────────────────────


class TestGetSprint:
    """Tests for ``GET /api/sprints/{id}``."""

    @pytest.mark.asyncio
    async def test_returns_sprint_with_repo(self, async_client, httpx_mock):
        readme_content = "# README"
        encoded = base64.b64encode(readme_content.encode()).decode()

        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)

        # Metadata refresh
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo",
            json={"full_name": "owner/test-repo", "description": "desc"},
        )
        # download_readme returns content
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo/readme",
            json={"content": encoded},
        )
        create_resp = await async_client.post(
            "/api/sprints",
            data={"name": "My Sprint", "repo_id": str(repo_id)},
        )
        sprint_id = create_resp.json()["id"]

        resp = await async_client.get(f"/api/sprints/{sprint_id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "My Sprint"
        assert data["repo"]["id"] == repo_id

    @pytest.mark.asyncio
    async def test_returns_404_for_nonexistent(self, async_client):
        resp = await async_client.get("/api/sprints/99999")
        assert resp.status_code == 404


# ── PATCH /api/sprints/{id} ──────────────────────────────────────────


class TestFinishSprint:
    """Tests for ``PATCH /api/sprints/{id}``."""

    @pytest.mark.asyncio
    async def test_finishes_sprint(self, async_client, httpx_mock):
        readme_content = "# README"
        encoded = base64.b64encode(readme_content.encode()).decode()

        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)

        # Metadata refresh
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo",
            json={"full_name": "owner/test-repo", "description": "desc"},
        )
        # download_readme returns content
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo/readme",
            json={"content": encoded},
        )
        create_resp = await async_client.post(
            "/api/sprints",
            data={"name": "Sprint", "repo_id": str(repo_id)},
        )
        sprint_id = create_resp.json()["id"]

        resp = await async_client.patch(
            f"/api/sprints/{sprint_id}",
            json={"active": False},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False

    @pytest.mark.asyncio
    async def test_rejects_reactivation(self, async_client, httpx_mock):
        readme_content = "# README"
        encoded = base64.b64encode(readme_content.encode()).decode()

        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)

        # Metadata refresh
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo",
            json={"full_name": "owner/test-repo", "description": "desc"},
        )
        # download_readme returns content
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo/readme",
            json={"content": encoded},
        )
        create_resp = await async_client.post(
            "/api/sprints",
            data={"name": "Sprint", "repo_id": str(repo_id)},
        )
        sprint_id = create_resp.json()["id"]

        resp = await async_client.patch(
            f"/api/sprints/{sprint_id}",
            json={"active": True},
        )

        assert resp.status_code == 422
        assert "Only transitioning active=False" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_double_finish(self, async_client, httpx_mock):
        readme_content = "# README"
        encoded = base64.b64encode(readme_content.encode()).decode()

        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)

        # Metadata refresh
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo",
            json={"full_name": "owner/test-repo", "description": "desc"},
        )
        # download_readme returns content
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo/readme",
            json={"content": encoded},
        )
        create_resp = await async_client.post(
            "/api/sprints",
            data={"name": "Sprint", "repo_id": str(repo_id)},
        )
        sprint_id = create_resp.json()["id"]

        await async_client.patch(f"/api/sprints/{sprint_id}", json={"active": False})
        resp = await async_client.patch(
            f"/api/sprints/{sprint_id}",
            json={"active": False},
        )

        assert resp.status_code == 422
        assert "already finished" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_returns_404_for_nonexistent(self, async_client):
        resp = await async_client.patch(
            "/api/sprints/99999",
            json={"active": False},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_marks_in_progress_requirements_failed(self, async_client, db_session):
        from backend.models.database import SPRINT_FINISHED_ERROR, Requirement, RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        pending = _seed_requirement(db_session, sprint)
        analyzing = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.ANALYZING,
            pending_answer="stale answer",
        )
        untouched = {
            _seed_requirement(db_session, sprint, status=status).id: status
            for status in (
                RequirementStatus.NEEDS_CLARIFICATION,
                RequirementStatus.READY,
                RequirementStatus.CONFIRMED,
                RequirementStatus.FAILED,
            )
        }

        resp = await async_client.patch(f"/api/sprints/{sprint.id}", json={"active": False})
        assert resp.status_code == 200

        db_session.expire_all()
        for req_id in (pending.id, analyzing.id):
            row = db_session.get(Requirement, req_id)
            assert row.status == RequirementStatus.FAILED
            assert row.error == SPRINT_FINISHED_ERROR
            assert row.last_heartbeat is None
            assert row.pending_answer is None
        for req_id, status in untouched.items():
            row = db_session.get(Requirement, req_id)
            assert row.status == status
            assert row.error is None

    @pytest.mark.asyncio
    async def test_marks_in_progress_test_plans_failed(self, async_client, db_session):
        from datetime import datetime, timezone

        from backend.models.database import (
            SPRINT_FINISHED_ERROR,
            RequirementStatus,
            TestPlan,
            TestPlanStatus,
        )
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)

        def _plan(status, **kwargs):
            requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
            return _seed_test_plan(db_session, requirement, status=status, **kwargs)

        pending = _plan(TestPlanStatus.PENDING, pending_feedback="stale feedback")
        generating = _plan(TestPlanStatus.GENERATING, last_heartbeat=datetime.now(timezone.utc))
        untouched = {
            _plan(status).id: status
            for status in (
                TestPlanStatus.DRAFT,
                TestPlanStatus.APPROVED,
                TestPlanStatus.FAILED,
            )
        }

        resp = await async_client.patch(f"/api/sprints/{sprint.id}", json={"active": False})
        assert resp.status_code == 200

        db_session.expire_all()
        for plan_id in (pending.id, generating.id):
            row = db_session.get(TestPlan, plan_id)
            assert row.status == TestPlanStatus.FAILED
            assert row.error == SPRINT_FINISHED_ERROR
            assert row.last_heartbeat is None
            assert row.pending_feedback is None
        for plan_id, status in untouched.items():
            row = db_session.get(TestPlan, plan_id)
            assert row.status == status
            assert row.error is None

    @pytest.mark.asyncio
    async def test_marks_in_progress_test_executions_failed(self, async_client, db_session):
        from datetime import datetime, timezone

        from backend.models.database import (
            SPRINT_FINISHED_ERROR,
            RequirementStatus,
            TestExecution,
            TestExecutionStatus,
        )
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)

        def _execution(status, **kwargs):
            requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
            run = _seed_test_run(db_session, sprint)
            return _seed_test_execution(db_session, run, requirement, status=status, **kwargs)

        pending = _execution(TestExecutionStatus.PENDING)
        running = _execution(TestExecutionStatus.RUNNING, last_heartbeat=datetime.now(timezone.utc))
        untouched = {
            _execution(status).id: status
            for status in (TestExecutionStatus.COMPLETED, TestExecutionStatus.FAILED)
        }

        resp = await async_client.patch(f"/api/sprints/{sprint.id}", json={"active": False})
        assert resp.status_code == 200

        db_session.expire_all()
        for execution_id in (pending.id, running.id):
            row = db_session.get(TestExecution, execution_id)
            assert row.status == TestExecutionStatus.FAILED
            assert row.error == SPRINT_FINISHED_ERROR
            assert row.last_heartbeat is None
        for execution_id, status in untouched.items():
            row = db_session.get(TestExecution, execution_id)
            assert row.status == status
            assert row.error is None


# == Repo file-tree capture during sprint creation ====================


class TestSprintFileTreeCapture:
    """The trees API is fetched best-effort during ``POST /api/sprints``."""

    @pytest.mark.asyncio
    async def test_stores_file_tree_on_repo(self, async_client, httpx_mock, db_session):
        readme = base64.b64encode(b"# README").decode()
        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)

        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo",
            json={"full_name": "owner/test-repo", "description": "d", "default_branch": "main"},
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo/git/trees/main?recursive=1",
            json={
                "tree": [
                    {"path": "src/app.py", "type": "blob"},
                    {"path": "logo.png", "type": "blob"},
                ],
                "truncated": False,
            },
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo/readme",
            json={"content": readme},
        )

        resp = await async_client.post(
            "/api/sprints",
            data={"name": "Sprint", "repo_id": str(repo_id)},
        )

        assert resp.status_code == 201
        from backend.models.database import Repo

        repo = db_session.get(Repo, repo_id)
        db_session.refresh(repo)
        assert repo.file_tree == "src/app.py"

    @pytest.mark.asyncio
    async def test_tree_fetch_failure_does_not_block_sprint(
        self, async_client, httpx_mock, db_session
    ):
        readme = base64.b64encode(b"# README").decode()
        repo_id = await _create_repo(async_client, "https://github.com/owner/test-repo", httpx_mock)

        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo",
            json={"full_name": "owner/test-repo", "description": "d", "default_branch": "main"},
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo/git/trees/main?recursive=1",
            status_code=500,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo/readme",
            json={"content": readme},
        )

        resp = await async_client.post(
            "/api/sprints",
            data={"name": "Sprint", "repo_id": str(repo_id)},
        )

        assert resp.status_code == 201
        from backend.models.database import Repo

        repo = db_session.get(Repo, repo_id)
        db_session.refresh(repo)
        assert repo.file_tree is None


# == Computed flags on SprintResponse =================================


def _seed_test_env(db_session, sprint, status=None, **kwargs):
    import json as _json

    from backend.models.database import TestEnvironmentAccess, TestEnvironmentStatus

    # A confirmed row always has extracted variables — confirm() refuses
    # without them — so seed a realistic one unless the test says otherwise.
    if status == TestEnvironmentStatus.CONFIRMED:
        kwargs.setdefault("env_vars_json", _json.dumps({"BASE_URL": "https://staging.example.com"}))
    row = TestEnvironmentAccess(
        sprint_id=sprint.id,
        content=kwargs.pop("content", "SSH into staging as qa@staging."),
        original_content=kwargs.pop("original_content", "SSH into staging as qa@staging."),
        status=status or TestEnvironmentStatus.NEEDS_INFO,
        **kwargs,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


class TestArchivedRequirements:
    """`Sprint.requirements` is the live view; archived rows stay in the table."""

    def test_archived_requirement_drops_out_of_the_collection(self, db_session):
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, name="Login")
        gone = _seed_requirement(db_session, sprint, name="Search")

        gone.archived = True
        db_session.add(gone)
        db_session.commit()
        db_session.refresh(sprint)

        assert [r.name for r in sprint.requirements] == ["Login"]
        assert {r.name for r in sprint.all_requirements} == {"Login", "Search"}

    def test_completion_flags_ignore_archived_rows(self, db_session):
        """An archived unconfirmed row must not hold the sprint back."""
        from backend.models.database import RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED, name="Login")
        pending = _seed_requirement(db_session, sprint, name="Search")  # not confirmed

        assert sprint.requirements_complete is False

        pending.archived = True
        db_session.add(pending)
        db_session.commit()
        db_session.refresh(sprint)

        assert sprint.requirements_complete is True

    @pytest.mark.asyncio
    async def test_archived_requirement_absent_from_the_list_endpoint(
        self, async_client, db_session
    ):
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, name="Login")
        gone = _seed_requirement(db_session, sprint, name="Search")
        gone.archived = True
        db_session.add(gone)
        db_session.commit()

        resp = await async_client.get(f"/api/sprints/{sprint.id}/requirements")

        assert resp.status_code == 200
        assert [row["name"] for row in resp.json()] == ["Login"]


class TestForeignKeyEnforcement:
    """The test database must reject dangling references, like PostgreSQL does.

    Without ``PRAGMA foreign_keys=ON`` SQLite silently accepts them, and a
    whole class of orphaning bug would pass the suite and fail in production.
    """

    def test_dangling_test_case_reference_is_rejected(self, db_session):
        import pytest as _pytest
        from sqlalchemy.exc import IntegrityError

        from backend.models.database import TestCaseExecution

        db_session.add(TestCaseExecution(test_execution_id=9999, test_case_id=9999))
        with _pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


class TestSprintFlags:
    """Serialization of the computed flags on list + detail endpoints."""

    @pytest.mark.asyncio
    async def test_flags_default_false_with_no_requirements(self, async_client, db_session):
        from backend.tests.test_requirement_routes import _seed_sprint

        sprint = _seed_sprint(db_session)

        for url in ("/api/sprints", f"/api/sprints/{sprint.id}"):
            resp = await async_client.get(url)
            assert resp.status_code == 200
            data = resp.json()
            row = data[0] if isinstance(data, list) else data
            assert row["requirements_complete"] is False
            assert row["has_test_environment_submission"] is False
            assert row["environment_confirmed"] is False

    @pytest.mark.asyncio
    async def test_requirements_complete_when_all_confirmed(self, async_client, db_session):
        from backend.models.database import RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED, name="Search")

        for url in ("/api/sprints", f"/api/sprints/{sprint.id}"):
            resp = await async_client.get(url)
            data = resp.json()
            row = data[0] if isinstance(data, list) else data
            assert row["requirements_complete"] is True

    @pytest.mark.asyncio
    async def test_requirements_incomplete_with_non_confirmed_row(self, async_client, db_session):
        from backend.models.database import RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        _seed_requirement(db_session, sprint, status=RequirementStatus.READY, name="Search")

        resp = await async_client.get(f"/api/sprints/{sprint.id}")
        assert resp.json()["requirements_complete"] is False

    @pytest.mark.asyncio
    async def test_submission_flag_without_lock(self, async_client, db_session):
        from backend.tests.test_requirement_routes import _seed_sprint

        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint)  # needs_info

        for url in ("/api/sprints", f"/api/sprints/{sprint.id}"):
            resp = await async_client.get(url)
            data = resp.json()
            row = data[0] if isinstance(data, list) else data
            assert row["has_test_environment_submission"] is True
            assert row["environment_confirmed"] is False

    @pytest.mark.asyncio
    async def test_locked_when_test_env_confirmed(self, async_client, db_session):
        from backend.models.database import TestEnvironmentStatus
        from backend.tests.test_requirement_routes import _seed_sprint

        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)

        for url in ("/api/sprints", f"/api/sprints/{sprint.id}"):
            resp = await async_client.get(url)
            data = resp.json()
            row = data[0] if isinstance(data, list) else data
            assert row["has_test_environment_submission"] is True
            assert row["environment_confirmed"] is True


class TestTestEnvironmentModelProperties:
    """Unit tests for the TestEnvironmentAccess computed properties."""

    @pytest.mark.parametrize(("revisions", "capped"), [(2, False), (3, True), (4, True)])
    def test_clarification_cap_reached(self, db_session, revisions, capped):
        from backend.tests.test_requirement_routes import _seed_sprint

        sprint = _seed_sprint(db_session)
        row = _seed_test_env(db_session, sprint, revision_count=revisions)
        assert row.clarification_cap_reached is capped

    def test_not_stale_when_confirmed_requirements_predate_check(self, db_session):
        from datetime import datetime, timezone

        from backend.models.database import RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        earlier = datetime(2026, 7, 1, tzinfo=timezone.utc)
        later = datetime(2026, 7, 2, tzinfo=timezone.utc)
        sprint = _seed_sprint(db_session)
        _seed_requirement(
            db_session, sprint, status=RequirementStatus.CONFIRMED, updated_at=earlier
        )
        row = _seed_test_env(db_session, sprint, updated_at=later)

        assert row.requirements_stale is False

    def test_stale_when_requirement_confirmed_after_check(self, db_session):
        from datetime import datetime, timezone

        from backend.models.database import RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        earlier = datetime(2026, 7, 1, tzinfo=timezone.utc)
        later = datetime(2026, 7, 2, tzinfo=timezone.utc)
        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED, updated_at=later)
        row = _seed_test_env(db_session, sprint, updated_at=earlier)

        assert row.requirements_stale is True

    def test_newer_non_confirmed_rows_do_not_trip_staleness(self, db_session):
        from datetime import datetime, timezone

        from backend.models.database import RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        earlier = datetime(2026, 7, 1, tzinfo=timezone.utc)
        later = datetime(2026, 7, 2, tzinfo=timezone.utc)
        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.READY, updated_at=later)
        row = _seed_test_env(db_session, sprint, updated_at=earlier)

        assert row.requirements_stale is False

    def test_equal_timestamps_are_not_stale(self, db_session):
        from datetime import datetime, timezone

        from backend.models.database import RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        moment = datetime(2026, 7, 1, tzinfo=timezone.utc)
        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED, updated_at=moment)
        row = _seed_test_env(db_session, sprint, updated_at=moment)

        assert row.requirements_stale is False


# == Test plan models + Sprint flags ===================================


def _seed_test_plan(db_session, requirement, status=None, **kwargs):
    from backend.models.database import TestPlan, TestPlanStatus

    plan = TestPlan(
        requirement_id=requirement.id,
        status=status or TestPlanStatus.PENDING,
        **kwargs,
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


def _seed_test_case(db_session, plan, position=0, **kwargs):
    from backend.models.database import TestCase

    case = TestCase(
        test_plan_id=plan.id,
        position=position,
        title=kwargs.pop("title", "Valid login"),
        steps=kwargs.pop("steps", "Open the login page\nSubmit valid credentials"),
        expected_result=kwargs.pop("expected_result", "User lands on the dashboard."),
        case_type=kwargs.pop("case_type", "functional"),
        priority=kwargs.pop("priority", "high"),
        **kwargs,
    )
    db_session.add(case)
    db_session.commit()
    db_session.refresh(case)
    return case


class TestTestPlanModelProperties:
    """Unit tests for the TestPlan computed properties and case cascade."""

    @pytest.mark.parametrize(("revisions", "capped"), [(2, False), (3, True), (4, True)])
    def test_feedback_cap_reached(self, db_session, revisions, capped):
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint)
        plan = _seed_test_plan(db_session, requirement, revision_count=revisions)

        assert plan.feedback_cap_reached is capped

    def test_requirement_name_and_description_passthrough(self, db_session):
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(
            db_session, sprint, name="Login", description="Users can log in."
        )
        plan = _seed_test_plan(db_session, requirement)

        assert plan.requirement_name == "Login"
        assert plan.requirement_description == "Users can log in."

    def test_requirement_fallbacks_empty_when_unloaded(self):
        from backend.models.database import TestPlan

        plan = TestPlan(requirement_id=1)

        assert plan.requirement_name == ""
        assert plan.requirement_description == ""

    def test_deleting_plan_detaches_cases_instead_of_deleting_them(self, db_session):
        """Cases outlive their plan — a past run still reads its content off them.

        The delete-orphan cascade that used to remove them is gone: a
        ``TestCaseExecution`` points at these rows, and deleting them would
        rewrite the record of a run that already happened.
        """
        from sqlmodel import select

        from backend.models.database import TestCase, TestPlan
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint)
        plan = _seed_test_plan(db_session, requirement)
        _seed_test_case(db_session, plan, position=0)
        _seed_test_case(db_session, plan, position=1, title="Invalid login")

        db_session.delete(plan)
        db_session.commit()

        assert db_session.get(TestPlan, plan.id) is None
        surviving = db_session.exec(select(TestCase)).all()
        assert len(surviving) == 2
        assert all(case.test_plan_id is None for case in surviving)

    def test_archived_cases_drop_out_of_plan_cases(self, db_session):
        """`cases` is the live view; `all_cases` is everything."""
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint)
        plan = _seed_test_plan(db_session, requirement)
        _seed_test_case(db_session, plan, position=0, title="Live")
        superseded = _seed_test_case(db_session, plan, position=1, title="Superseded")

        superseded.archived = True
        db_session.add(superseded)
        db_session.commit()
        db_session.refresh(plan)

        assert [c.title for c in plan.cases] == ["Live"]
        assert {c.title for c in plan.all_cases} == {"Live", "Superseded"}

    def test_cases_ordered_by_position(self, db_session):
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint)
        plan = _seed_test_plan(db_session, requirement)
        second = _seed_test_case(db_session, plan, position=1, title="Second")
        first = _seed_test_case(db_session, plan, position=0, title="First")

        db_session.refresh(plan)
        assert [c.id for c in plan.cases] == [first.id, second.id]


class TestSprintTestPlanFlags:
    """`has_test_plans` / `test_plans_complete` — model values and serialization."""

    def test_false_with_no_requirements(self, db_session):
        from backend.tests.test_requirement_routes import _seed_sprint

        sprint = _seed_sprint(db_session)

        assert sprint.has_test_plans is False
        assert sprint.test_plans_complete is False

    def test_false_with_requirements_but_no_plans(self, db_session):
        from backend.models.database import RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)

        db_session.refresh(sprint)
        assert sprint.has_test_plans is False
        assert sprint.test_plans_complete is False

    def test_has_test_plans_with_one_plan(self, db_session):
        from backend.models.database import RequirementStatus, TestPlanStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        planned = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED, name="Search")
        _seed_test_plan(db_session, planned, status=TestPlanStatus.DRAFT)

        db_session.refresh(sprint)
        assert sprint.has_test_plans is True
        assert sprint.test_plans_complete is False

    def test_incomplete_while_any_plan_not_approved(self, db_session):
        from backend.models.database import RequirementStatus, TestPlanStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        approved = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        draft = _seed_requirement(
            db_session, sprint, status=RequirementStatus.CONFIRMED, name="Search"
        )
        _seed_test_plan(db_session, approved, status=TestPlanStatus.APPROVED)
        _seed_test_plan(db_session, draft, status=TestPlanStatus.DRAFT)

        db_session.refresh(sprint)
        assert sprint.has_test_plans is True
        assert sprint.test_plans_complete is False

    def test_complete_when_all_plans_approved(self, db_session):
        from backend.models.database import RequirementStatus, TestPlanStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        for name in ("Login", "Search"):
            requirement = _seed_requirement(
                db_session, sprint, status=RequirementStatus.CONFIRMED, name=name
            )
            _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED)

        db_session.refresh(sprint)
        assert sprint.has_test_plans is True
        assert sprint.test_plans_complete is True

    @pytest.mark.asyncio
    async def test_flags_serialized_on_list_and_detail(self, async_client, db_session):
        from backend.models.database import RequirementStatus, TestPlanStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)

        for url in ("/api/sprints", f"/api/sprints/{sprint.id}"):
            resp = await async_client.get(url)
            assert resp.status_code == 200
            data = resp.json()
            row = data[0] if isinstance(data, list) else data
            assert row["has_test_plans"] is False
            assert row["test_plans_complete"] is False

        plan = _seed_test_plan(db_session, requirement, status=TestPlanStatus.DRAFT)

        for url in ("/api/sprints", f"/api/sprints/{sprint.id}"):
            resp = await async_client.get(url)
            data = resp.json()
            row = data[0] if isinstance(data, list) else data
            assert row["has_test_plans"] is True
            assert row["test_plans_complete"] is False

        plan.status = TestPlanStatus.APPROVED
        db_session.add(plan)
        db_session.commit()

        for url in ("/api/sprints", f"/api/sprints/{sprint.id}"):
            resp = await async_client.get(url)
            data = resp.json()
            row = data[0] if isinstance(data, list) else data
            assert row["has_test_plans"] is True
            assert row["test_plans_complete"] is True


# == Test execution models + Sprint flag ================================


def _seed_test_run(db_session, sprint, **kwargs):
    from backend.models.database import TestRun

    run = TestRun(sprint_id=sprint.id, **kwargs)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _seed_test_execution(db_session, test_run, requirement, status=None, **kwargs):
    from backend.models.database import TestExecution, TestExecutionStatus

    execution = TestExecution(
        test_run_id=test_run.id,
        requirement_id=requirement.id,
        status=status or TestExecutionStatus.PENDING,
        **kwargs,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)
    return execution


def _seed_test_case_execution(db_session, test_execution, test_case, status=None, **kwargs):
    from backend.models.database import TestCaseExecution, TestCaseExecutionStatus

    case_execution = TestCaseExecution(
        test_execution_id=test_execution.id,
        test_case_id=test_case.id,
        status=status or TestCaseExecutionStatus.PENDING,
        **kwargs,
    )
    db_session.add(case_execution)
    db_session.commit()
    db_session.refresh(case_execution)
    return case_execution


class TestTestRunModelProperties:
    """Unit tests for TestRun.status rollup and requirement_names."""

    def test_status_completed_with_no_executions(self, db_session):
        from backend.tests.test_requirement_routes import _seed_sprint

        sprint = _seed_sprint(db_session)
        run = _seed_test_run(db_session, sprint)

        assert run.status == "completed"
        assert run.requirement_names == []

    @pytest.mark.parametrize(
        ("statuses", "expected"),
        [
            (["completed", "completed"], "completed"),
            (["completed", "failed"], "failed"),
            (["pending", "completed"], "running"),
            (["running", "failed"], "running"),
            (["failed", "pending"], "running"),
        ],
    )
    def test_status_rollup(self, db_session, statuses, expected):
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        run = _seed_test_run(db_session, sprint)
        for i, status in enumerate(statuses):
            requirement = _seed_requirement(db_session, sprint, name=f"Req {i}")
            _seed_test_execution(db_session, run, requirement, status=status)

        db_session.refresh(run)
        assert run.status == expected

    def test_requirement_names_ordering(self, db_session):
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        run = _seed_test_run(db_session, sprint)
        first = _seed_requirement(db_session, sprint, name="Login")
        second = _seed_requirement(db_session, sprint, name="Search")
        _seed_test_execution(db_session, run, first)
        _seed_test_execution(db_session, run, second)

        db_session.refresh(run)
        assert run.requirement_names == ["Login", "Search"]

    def test_deleting_run_cascades_to_executions_and_cases(self, db_session):
        from sqlmodel import select

        from backend.models.database import TestCaseExecution, TestExecution, TestRun
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint)
        plan = _seed_test_plan(db_session, requirement)
        case = _seed_test_case(db_session, plan)
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(db_session, run, requirement)
        _seed_test_case_execution(db_session, execution, case)

        db_session.delete(run)
        db_session.commit()

        assert db_session.get(TestRun, run.id) is None
        assert db_session.exec(select(TestExecution)).all() == []
        assert db_session.exec(select(TestCaseExecution)).all() == []


class TestSprintTestRunFlag:
    """`has_test_runs` — model value and serialization."""

    def test_false_with_no_runs(self, db_session):
        from backend.tests.test_requirement_routes import _seed_sprint

        sprint = _seed_sprint(db_session)
        assert sprint.has_test_runs is False

    def test_true_with_one_run(self, db_session):
        from backend.tests.test_requirement_routes import _seed_sprint

        sprint = _seed_sprint(db_session)
        _seed_test_run(db_session, sprint)

        db_session.refresh(sprint)
        assert sprint.has_test_runs is True

    @pytest.mark.asyncio
    async def test_flag_serialized_on_list_and_detail(self, async_client, db_session):
        from backend.tests.test_requirement_routes import _seed_sprint

        sprint = _seed_sprint(db_session)

        for url in ("/api/sprints", f"/api/sprints/{sprint.id}"):
            resp = await async_client.get(url)
            assert resp.status_code == 200
            data = resp.json()
            row = data[0] if isinstance(data, list) else data
            assert row["has_test_runs"] is False

        _seed_test_run(db_session, sprint)

        for url in ("/api/sprints", f"/api/sprints/{sprint.id}"):
            resp = await async_client.get(url)
            data = resp.json()
            row = data[0] if isinstance(data, list) else data
            assert row["has_test_runs"] is True


# == Exploratory testing model ========================================


def _seed_exploratory_run(db_session, sprint, requirement, **kwargs):
    from backend.models.database import ExploratoryRun

    kwargs.setdefault("base_url_env_vars_csv", "APP_URL")
    run = ExploratoryRun(sprint_id=sprint.id, requirement_id=requirement.id, **kwargs)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _seed_exploratory_session(db_session, run, position=0, **kwargs):
    from backend.models.database import ExploratorySession

    kwargs.setdefault("charter", "Explore the export flow")
    kwargs.setdefault("sfdipot_areas_csv", "Function,Data")
    session = ExploratorySession(exploratory_run_id=run.id, position=position, **kwargs)
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    return session


def _seed_exploratory_finding(db_session, session, position=0, **kwargs):
    from backend.models.database import ExploratoryFinding, FindingSeverity, FindingType

    kwargs.setdefault("finding_type", FindingType.BUG)
    kwargs.setdefault("severity", FindingSeverity.HIGH)
    kwargs.setdefault("title", "Export produces an empty file")
    kwargs.setdefault("steps_to_reproduce", "Open reports\nClick Export")
    kwargs.setdefault("expected", "A CSV with a header row")
    kwargs.setdefault("actual", "A zero-byte file")
    finding = ExploratoryFinding(exploratory_session_id=session.id, position=position, **kwargs)
    db_session.add(finding)
    db_session.commit()
    db_session.refresh(finding)
    return finding


class TestExploratoryModel:
    """Comma-joined column accessors, ordering, and cascade behaviour."""

    def _sprint_and_requirement(self, db_session):
        from backend.models.database import RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        return sprint, requirement

    def test_base_url_env_vars_splits_multiple(self, db_session):
        sprint, requirement = self._sprint_and_requirement(db_session)
        run = _seed_exploratory_run(
            db_session, sprint, requirement, base_url_env_vars_csv="APP_URL,API_URL"
        )
        assert run.base_url_env_vars == ["APP_URL", "API_URL"]

    def test_base_url_env_vars_splits_single(self, db_session):
        sprint, requirement = self._sprint_and_requirement(db_session)
        run = _seed_exploratory_run(
            db_session, sprint, requirement, base_url_env_vars_csv="APP_URL"
        )
        assert run.base_url_env_vars == ["APP_URL"]

    def test_base_url_env_vars_empty_string_yields_empty_list(self, db_session):
        """A naive ``"".split(",")`` returns ``[""]`` — the property must not."""
        sprint, requirement = self._sprint_and_requirement(db_session)
        run = _seed_exploratory_run(db_session, sprint, requirement, base_url_env_vars_csv="")
        assert run.base_url_env_vars == []

    def test_sfdipot_areas_splits(self, db_session):
        sprint, requirement = self._sprint_and_requirement(db_session)
        run = _seed_exploratory_run(db_session, sprint, requirement)
        session = _seed_exploratory_session(
            db_session, run, sfdipot_areas_csv="Function,Data,Interfaces"
        )
        assert session.sfdipot_areas == ["Function", "Data", "Interfaces"]

    def test_sfdipot_areas_empty_string_yields_empty_list(self, db_session):
        sprint, requirement = self._sprint_and_requirement(db_session)
        run = _seed_exploratory_run(db_session, sprint, requirement)
        session = _seed_exploratory_session(db_session, run, sfdipot_areas_csv="")
        assert session.sfdipot_areas == []

    def test_requirement_name_property(self, db_session):
        sprint, requirement = self._sprint_and_requirement(db_session)
        run = _seed_exploratory_run(db_session, sprint, requirement)
        db_session.refresh(run)
        assert run.requirement_name == requirement.name

    def test_sessions_ordered_by_position(self, db_session):
        sprint, requirement = self._sprint_and_requirement(db_session)
        run = _seed_exploratory_run(db_session, sprint, requirement)
        _seed_exploratory_session(db_session, run, position=2, charter="third")
        _seed_exploratory_session(db_session, run, position=0, charter="first")
        _seed_exploratory_session(db_session, run, position=1, charter="second")

        db_session.refresh(run)
        assert [s.charter for s in run.sessions] == ["first", "second", "third"]

    def test_delete_run_cascades_to_sessions_and_findings(self, db_session):
        from sqlmodel import select

        from backend.models.database import (
            ExploratoryFinding,
            ExploratoryRun,
            ExploratorySession,
        )

        sprint, requirement = self._sprint_and_requirement(db_session)
        run = _seed_exploratory_run(db_session, sprint, requirement)
        session = _seed_exploratory_session(db_session, run)
        _seed_exploratory_finding(db_session, session)

        run_id = run.id
        db_session.delete(run)
        db_session.commit()

        assert db_session.get(ExploratoryRun, run_id) is None
        assert db_session.exec(select(ExploratorySession)).all() == []
        assert db_session.exec(select(ExploratoryFinding)).all() == []


class TestSprintExploratoryFlag:
    """`has_exploratory_runs` — model value and serialization."""

    def test_false_with_no_runs(self, db_session):
        from backend.tests.test_requirement_routes import _seed_sprint

        sprint = _seed_sprint(db_session)
        assert sprint.has_exploratory_runs is False

    def test_true_with_one_run(self, db_session):
        from backend.models.database import RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        _seed_exploratory_run(db_session, sprint, requirement)

        db_session.refresh(sprint)
        assert sprint.has_exploratory_runs is True

    @pytest.mark.asyncio
    async def test_flag_serialized_on_list_and_detail(self, async_client, db_session):
        from backend.models.database import RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)

        for url in ("/api/sprints", f"/api/sprints/{sprint.id}"):
            resp = await async_client.get(url)
            assert resp.status_code == 200
            data = resp.json()
            row = data[0] if isinstance(data, list) else data
            assert row["has_exploratory_runs"] is False

        _seed_exploratory_run(db_session, sprint, requirement)

        for url in ("/api/sprints", f"/api/sprints/{sprint.id}"):
            resp = await async_client.get(url)
            data = resp.json()
            row = data[0] if isinstance(data, list) else data
            assert row["has_exploratory_runs"] is True


class TestFinishSprintSweepsExploratoryRuns:
    """Finishing a sprint must not leave an exploration in progress."""

    def _run(self, db_session, sprint, status=None, **kwargs):
        from backend.models.database import RequirementStatus
        from backend.tests.test_requirement_routes import _seed_requirement

        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        if status is not None:
            kwargs["status"] = status
        return _seed_exploratory_run(db_session, sprint, requirement, **kwargs)

    @pytest.mark.asyncio
    async def test_fails_in_progress_runs(self, async_client, db_session):
        from datetime import datetime, timezone

        from backend.models.database import (
            SPRINT_FINISHED_ERROR,
            ExploratoryRun,
            ExploratoryRunStatus,
        )
        from backend.tests.test_requirement_routes import _seed_sprint

        sprint = _seed_sprint(db_session)
        pending = self._run(db_session, sprint)
        running = self._run(
            db_session,
            sprint,
            status=ExploratoryRunStatus.RUNNING,
            last_heartbeat=datetime.now(timezone.utc),
        )

        resp = await async_client.patch(f"/api/sprints/{sprint.id}", json={"active": False})
        assert resp.status_code == 200

        db_session.expire_all()
        for run_id in (pending.id, running.id):
            row = db_session.get(ExploratoryRun, run_id)
            assert row.status == ExploratoryRunStatus.FAILED
            assert row.error == SPRINT_FINISHED_ERROR
            assert row.last_heartbeat is None

    @pytest.mark.asyncio
    async def test_settled_runs_untouched(self, async_client, db_session):
        from backend.models.database import ExploratoryRun, ExploratoryRunStatus
        from backend.tests.test_requirement_routes import _seed_sprint

        sprint = _seed_sprint(db_session)
        settled = {
            self._run(db_session, sprint, status=status).id: status
            for status in (ExploratoryRunStatus.COMPLETED, ExploratoryRunStatus.FAILED)
        }

        resp = await async_client.patch(f"/api/sprints/{sprint.id}", json={"active": False})
        assert resp.status_code == 200

        db_session.expire_all()
        for run_id, status in settled.items():
            row = db_session.get(ExploratoryRun, run_id)
            assert row.status == status
            assert row.error is None
