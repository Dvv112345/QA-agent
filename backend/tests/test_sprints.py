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
