"""Tests for backend/routes/sprints.py — sprint CRUD and README resolution."""

import base64

import pytest

from backend.tests.conftest import _create_repo  # noqa: F401 — used by tests

# ── POST /api/sprints ────────────────────────────────────────────────


class TestCreateSprint:
    """Tests for ``POST /api/sprints``."""

    @pytest.mark.asyncio
    async def test_creates_sprint_with_github_readme(self, async_client, httpx_mock):
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

    @pytest.mark.asyncio
    async def test_creates_sprint_with_user_readme(self, async_client, httpx_mock):
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
    from backend.models.database import TestEnvironmentAccess, TestEnvironmentStatus

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
            assert row["requirements_locked"] is False

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
            assert row["requirements_locked"] is False

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
            assert row["requirements_locked"] is True


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
