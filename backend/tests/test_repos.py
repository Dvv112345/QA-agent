"""Tests for backend/routes/repos.py — repo CRUD and README status."""

import importlib

import pytest
from httpx import ASGITransport, AsyncClient


def _make_client(monkeypatch, db_session):
    """Create an async test client with auth disabled and in-memory SQLite."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.setenv("STORE_OFFLINE", "false")
    monkeypatch.delenv("STORAGE_LOCATION", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)

    import backend.config
    import backend.main

    importlib.reload(backend.config)
    importlib.reload(backend.main)

    monkeypatch.setattr(backend.main, "_check_redis_health", lambda: "mocked")

    from backend.database import get_session
    from backend.main import create_app

    async def _override():
        return db_session

    app = create_app()
    app.dependency_overrides[get_session] = _override

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ── POST /api/repos ──────────────────────────────────────────────────


class TestCreateRepo:
    """Tests for ``POST /api/repos``."""

    @pytest.mark.asyncio
    async def test_creates_repo_successfully(self, monkeypatch, db_session, httpx_mock):
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
        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.post(
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

    @pytest.mark.asyncio
    async def test_creates_repo_with_token(self, monkeypatch, db_session, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/private-repo",
            json={"full_name": "owner/private-repo"},
        )
        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.post(
                "/api/repos",
                data={
                    "github_url": "https://github.com/owner/private-repo",
                    "access_token": "ghp_test123",
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "owner/private-repo"
        assert "github_token" not in data

    @pytest.mark.asyncio
    async def test_rejects_invalid_github_url(self, monkeypatch, db_session):
        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.post(
                "/api/repos",
                data={"github_url": "not-a-valid-url"},
            )
        assert resp.status_code == 422
        assert "Invalid GitHub repository URL" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_inaccessible_repo(self, monkeypatch, db_session, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/nope",
            status_code=404,
        )
        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.post(
                "/api/repos",
                data={"github_url": "https://github.com/owner/nope"},
            )
        assert resp.status_code == 422


# ── GET /api/repos ───────────────────────────────────────────────────


class TestListRepos:
    """Tests for ``GET /api/repos``."""

    @pytest.mark.asyncio
    async def test_returns_active_repos_only(self, monkeypatch, db_session, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1",
            json={"full_name": "owner/repo1"},
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo2",
            json={"full_name": "owner/repo2"},
        )
        async with _make_client(monkeypatch, db_session) as client:
            await client.post("/api/repos", data={"github_url": "https://github.com/owner/repo1"})
            await client.post("/api/repos", data={"github_url": "https://github.com/owner/repo2"})

            resp = await client.get("/api/repos")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert all(r["active"] for r in data)

    @pytest.mark.asyncio
    async def test_excludes_deactivated_repos(self, monkeypatch, db_session, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1",
            json={"full_name": "owner/repo1", "description": "desc"},
        )
        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.post(
                "/api/repos",
                data={"github_url": "https://github.com/owner/repo1"},
            )
            repo_id = resp.json()["id"]
            await client.post(f"/api/repos/{repo_id}/deactivate")

            resp = await client.get("/api/repos")
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_repos(self, monkeypatch, db_session):
        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.get("/api/repos")
        assert resp.status_code == 200
        assert resp.json() == []


# ── POST /api/repos/{id}/deactivate ──────────────────────────────────


class TestDeactivateRepo:
    """Tests for ``POST /api/repos/{id}/deactivate``."""

    @pytest.mark.asyncio
    async def test_deactivates_repo(self, monkeypatch, db_session, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1",
            json={"full_name": "owner/repo1", "description": "desc"},
        )
        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.post(
                "/api/repos",
                data={"github_url": "https://github.com/owner/repo1"},
            )
            repo_id = resp.json()["id"]

            resp = await client.post(f"/api/repos/{repo_id}/deactivate")
        assert resp.status_code == 200
        assert resp.json() == {"deactivated": True}

    @pytest.mark.asyncio
    async def test_returns_404_for_nonexistent_repo(self, monkeypatch, db_session):
        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.post("/api/repos/99999/deactivate")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rejects_double_deactivation(self, monkeypatch, db_session, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1",
            json={"full_name": "owner/repo1", "description": "desc"},
        )
        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.post(
                "/api/repos",
                data={"github_url": "https://github.com/owner/repo1"},
            )
            repo_id = resp.json()["id"]

            await client.post(f"/api/repos/{repo_id}/deactivate")
            resp = await client.post(f"/api/repos/{repo_id}/deactivate")
        assert resp.status_code == 422
        assert "already deactivated" in resp.json()["detail"]


# ── GET /api/repos/{id}/readme-status ────────────────────────────────


class TestReadmeStatus:
    """Tests for ``GET /api/repos/{id}/readme-status``."""

    @pytest.mark.asyncio
    async def test_has_readme_true(self, monkeypatch, db_session, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1",
            json={"full_name": "owner/repo1", "description": "desc"},
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1/readme",
            status_code=200,
        )
        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.post(
                "/api/repos",
                data={"github_url": "https://github.com/owner/repo1"},
            )
            repo_id = resp.json()["id"]

            resp = await client.get(f"/api/repos/{repo_id}/readme-status")
        assert resp.status_code == 200
        assert resp.json() == {"has_readme": True}

    @pytest.mark.asyncio
    async def test_has_readme_false(self, monkeypatch, db_session, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1",
            json={"full_name": "owner/repo1", "description": "desc"},
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo1/readme",
            status_code=404,
        )
        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.post(
                "/api/repos",
                data={"github_url": "https://github.com/owner/repo1"},
            )
            repo_id = resp.json()["id"]

            resp = await client.get(f"/api/repos/{repo_id}/readme-status")
        assert resp.status_code == 200
        assert resp.json() == {"has_readme": False}

    @pytest.mark.asyncio
    async def test_returns_404_for_nonexistent_repo(self, monkeypatch, db_session):
        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.get("/api/repos/99999/readme-status")
        assert resp.status_code == 404
