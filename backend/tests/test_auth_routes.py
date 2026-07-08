"""Integration tests for the /api/auth/verify and /api/auth/check endpoints."""

import importlib

import pytest
from httpx import ASGITransport, AsyncClient


def _make_client(monkeypatch, db_session, *, password: str | None = "secret123"):
    """Create an async test client with APP_PASSWORD configured."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.setenv("STORE_OFFLINE", "false")
    monkeypatch.delenv("STORAGE_LOCATION", raising=False)

    if password is None:
        monkeypatch.delenv("APP_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("APP_PASSWORD", password)

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


# ── POST /api/auth/verify ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_correct_password(monkeypatch, db_session):
    async with _make_client(monkeypatch, db_session) as client:
        resp = await client.post("/api/auth/verify", json={"password": "secret123"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    set_cookie = resp.headers.get("set-cookie", "")
    assert "qa_auth=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert (
        "SameSite=Strict" in set_cookie
        or "SameSite=Lax" in set_cookie
        or "samesite" in set_cookie.lower()
    )


@pytest.mark.asyncio
async def test_verify_wrong_password(monkeypatch, db_session):
    async with _make_client(monkeypatch, db_session) as client:
        resp = await client.post("/api/auth/verify", json={"password": "wrong"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert "set-cookie" not in resp.headers


@pytest.mark.asyncio
async def test_verify_empty_body_when_auth_enabled(monkeypatch, db_session):
    async with _make_client(monkeypatch, db_session) as client:
        resp = await client.post("/api/auth/verify", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_verify_when_auth_disabled(monkeypatch, db_session):
    async with _make_client(monkeypatch, db_session, password=None) as client:
        resp = await client.post("/api/auth/verify", json={"password": "anything"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert "set-cookie" not in resp.headers


# ── GET /api/auth/check ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_with_valid_cookie(monkeypatch, db_session):
    async with _make_client(monkeypatch, db_session) as client:
        resp = await client.get("/api/auth/check", cookies={"qa_auth": "secret123"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True


@pytest.mark.asyncio
async def test_check_with_invalid_cookie(monkeypatch, db_session):
    async with _make_client(monkeypatch, db_session) as client:
        resp = await client.get("/api/auth/check", cookies={"qa_auth": "wrong-value"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False


@pytest.mark.asyncio
async def test_check_with_no_cookie(monkeypatch, db_session):
    async with _make_client(monkeypatch, db_session) as client:
        resp = await client.get("/api/auth/check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False


@pytest.mark.asyncio
async def test_check_when_auth_disabled(monkeypatch, db_session):
    async with _make_client(monkeypatch, db_session, password=None) as client:
        resp = await client.get("/api/auth/check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True


# ── Protected route tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_works_without_cookie(monkeypatch, db_session):
    async with _make_client(monkeypatch, db_session) as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_protected_route_rejected_without_cookie(monkeypatch, db_session):
    async with _make_client(monkeypatch, db_session) as client:
        resp = await client.post("/api/repos")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_accepted_with_valid_cookie(monkeypatch, db_session):
    async with _make_client(monkeypatch, db_session) as client:
        resp = await client.post("/api/repos", cookies={"qa_auth": "secret123"})
    # 422 = validation error (missing fields), but NOT 401
    assert resp.status_code == 422
