"""Tests for backend/routes/repos.py — repo CRUD and README status."""

import asyncio

import pytest

# ── POST /api/repos ──────────────────────────────────────────────────


class TestCreateRepo:
    """Tests for ``POST /api/repos``."""

    @pytest.mark.asyncio
    async def test_creates_repo_successfully(self, async_client, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/test-repo",
            json={
                "full_name": "owner/test-repo",
                "description": "A test repository",
                "private": False,
                "clone_url": "https://github.com/owner/test-repo.git",
                "default_branch": "main",
            },
        )
        resp = await async_client.post(
            "/api/repos",
            data={"github_url": "https://github.com/owner/test-repo"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "owner/test-repo"
        assert data["description"] == "A test repository"
        assert data["active"] is True
        assert data["github_link"] == "https://github.com/owner/test-repo"
        assert "github_token" not in data
        assert data["has_access_token"] is False

    @pytest.mark.asyncio
    async def test_creates_repo_with_token(self, async_client, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/private-repo",
            json={"full_name": "owner/private-repo"},
        )
        resp = await async_client.post(
            "/api/repos",
            data={
                "github_url": "https://github.com/owner/private-repo",
                "access_token": "ghp_test123",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "owner/private-repo"
        # Whether a token exists is visible; the token itself never is —
        # the issue-tracker form needs the first and must never see the second.
        assert data["has_access_token"] is True
        assert "github_token" not in data

    @pytest.mark.asyncio
    async def test_rejects_invalid_github_url(self, async_client):
        resp = await async_client.post(
            "/api/repos",
            data={"github_url": "not-a-valid-url"},
        )
        assert resp.status_code == 422
        assert "Invalid GitHub repository URL" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_inaccessible_repo(self, async_client, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/nope",
            status_code=404,
        )
        resp = await async_client.post(
            "/api/repos",
            data={"github_url": "https://github.com/owner/nope"},
        )
        assert resp.status_code == 422


# ── GET /api/repos ───────────────────────────────────────────────────


class TestListRepos:
    """Tests for ``GET /api/repos``."""

    @pytest.mark.asyncio
    async def test_returns_active_repos_only(self, async_client, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1",
            json={"full_name": "owner/repo1"},
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo2",
            json={"full_name": "owner/repo2"},
        )
        await async_client.post("/api/repos", data={"github_url": "https://github.com/owner/repo1"})
        await async_client.post("/api/repos", data={"github_url": "https://github.com/owner/repo2"})

        resp = await async_client.get("/api/repos")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert all(r["active"] for r in data)

    @pytest.mark.asyncio
    async def test_excludes_deactivated_repos(self, async_client, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1",
            json={"full_name": "owner/repo1", "description": "desc"},
        )
        resp = await async_client.post(
            "/api/repos",
            data={"github_url": "https://github.com/owner/repo1"},
        )
        repo_id = resp.json()["id"]
        await async_client.post(f"/api/repos/{repo_id}/deactivate")

        resp = await async_client.get("/api/repos")
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_repos(self, async_client):
        resp = await async_client.get("/api/repos")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_respects_limit(self, async_client, httpx_mock):
        for i in range(3):
            httpx_mock.add_response(
                url=f"https://api.github.com/repos/owner/repo{i}",
                json={"full_name": f"owner/repo{i}"},
            )
            await async_client.post(
                "/api/repos",
                data={"github_url": f"https://github.com/owner/repo{i}"},
            )
        resp = await async_client.get("/api/repos?limit=2")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_respects_offset(self, async_client, httpx_mock):
        for i in range(3):
            httpx_mock.add_response(
                url=f"https://api.github.com/repos/owner/repo{i}",
                json={"full_name": f"owner/repo{i}"},
            )
            await async_client.post(
                "/api/repos",
                data={"github_url": f"https://github.com/owner/repo{i}"},
            )
            await asyncio.sleep(1)
        resp = await async_client.get("/api/repos?offset=1&limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Newest first, so skipping first gives repos 1 and 0
        assert data[0]["name"] == "owner/repo1"


# ── POST /api/repos/{id}/deactivate ──────────────────────────────────


class TestDeactivateRepo:
    """Tests for ``POST /api/repos/{id}/deactivate``."""

    @pytest.mark.asyncio
    async def test_deactivates_repo(self, async_client, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1",
            json={"full_name": "owner/repo1", "description": "desc"},
        )
        resp = await async_client.post(
            "/api/repos",
            data={"github_url": "https://github.com/owner/repo1"},
        )
        repo_id = resp.json()["id"]

        resp = await async_client.post(f"/api/repos/{repo_id}/deactivate")
        assert resp.status_code == 200
        assert resp.json() == {"deactivated": True}

    @pytest.mark.asyncio
    async def test_returns_404_for_nonexistent_repo(self, async_client):
        resp = await async_client.post("/api/repos/99999/deactivate")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rejects_double_deactivation(self, async_client, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1",
            json={"full_name": "owner/repo1", "description": "desc"},
        )
        resp = await async_client.post(
            "/api/repos",
            data={"github_url": "https://github.com/owner/repo1"},
        )
        repo_id = resp.json()["id"]

        await async_client.post(f"/api/repos/{repo_id}/deactivate")
        resp = await async_client.post(f"/api/repos/{repo_id}/deactivate")
        assert resp.status_code == 422
        assert "already deactivated" in resp.json()["detail"]


# ── GET /api/repos/{id}/readme-status ────────────────────────────────


class TestReadmeStatus:
    """Tests for ``GET /api/repos/{id}/readme-status``."""

    @pytest.mark.asyncio
    async def test_has_readme_true(self, async_client, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1",
            json={"full_name": "owner/repo1", "description": "desc"},
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1/readme",
            status_code=200,
        )
        resp = await async_client.post(
            "/api/repos",
            data={"github_url": "https://github.com/owner/repo1"},
        )
        repo_id = resp.json()["id"]

        resp = await async_client.get(f"/api/repos/{repo_id}/readme-status")
        assert resp.status_code == 200
        assert resp.json() == {"has_readme": True}

    @pytest.mark.asyncio
    async def test_has_readme_false(self, async_client, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1",
            json={"full_name": "owner/repo1", "description": "desc"},
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1/readme",
            status_code=404,
        )
        resp = await async_client.post(
            "/api/repos",
            data={"github_url": "https://github.com/owner/repo1"},
        )
        repo_id = resp.json()["id"]

        resp = await async_client.get(f"/api/repos/{repo_id}/readme-status")
        assert resp.status_code == 200
        assert resp.json() == {"has_readme": False}

    @pytest.mark.asyncio
    async def test_returns_404_for_nonexistent_repo(self, async_client):
        resp = await async_client.get("/api/repos/99999/readme-status")
        assert resp.status_code == 404
