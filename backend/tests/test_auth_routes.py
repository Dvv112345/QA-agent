"""Integration tests for the /api/auth/verify and /api/auth/check endpoints."""

import importlib

import pytest
from httpx import ASGITransport, AsyncClient


def _make_client(monkeypatch, *, password: str | None = "secret123"):
    """Create an async test client with APP_PASSWORD configured.

    Pass ``password=None`` to simulate auth-disabled mode (APP_PASSWORD unset).
    """
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.setenv("STORE_OFFLINE", "false")
    monkeypatch.setenv("MAX_ZIP_FILES", "100000")
    monkeypatch.delenv("STORAGE_LOCATION", raising=False)

    if password is None:
        monkeypatch.delenv("APP_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("APP_PASSWORD", password)

    import backend.config
    import backend.main

    importlib.reload(backend.config)
    importlib.reload(backend.main)

    # Prevent any test from accessing a real Redis server
    monkeypatch.setattr(backend.main, "_check_redis_health", lambda: "mocked")

    from backend.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ── POST /api/auth/verify ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_verify_correct_password(monkeypatch):
    """Correct password returns {valid: true} and sets HttpOnly cookie."""
    async with _make_client(monkeypatch) as client:
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


@pytest.mark.anyio
async def test_verify_wrong_password(monkeypatch):
    """Wrong password returns {valid: false} and does NOT set a cookie."""
    async with _make_client(monkeypatch) as client:
        resp = await client.post("/api/auth/verify", json={"password": "wrong"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert "set-cookie" not in resp.headers


@pytest.mark.anyio
async def test_verify_empty_body_when_auth_enabled(monkeypatch):
    """Missing password field returns 422 validation error."""
    async with _make_client(monkeypatch) as client:
        resp = await client.post("/api/auth/verify", json={})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_verify_when_auth_disabled(monkeypatch):
    """When APP_PASSWORD is unset, verify always returns {valid: true}."""
    async with _make_client(monkeypatch, password=None) as client:
        resp = await client.post("/api/auth/verify", json={"password": "anything"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert "set-cookie" not in resp.headers


# ── GET /api/auth/check ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_check_with_valid_cookie(monkeypatch):
    """A request with the correct cookie returns {valid: true}."""
    async with _make_client(monkeypatch) as client:
        resp = await client.get("/api/auth/check", cookies={"qa_auth": "secret123"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True


@pytest.mark.anyio
async def test_check_with_invalid_cookie(monkeypatch):
    """A request with the wrong cookie returns {valid: false} (NOT 401)."""
    async with _make_client(monkeypatch) as client:
        resp = await client.get("/api/auth/check", cookies={"qa_auth": "wrong-value"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False


@pytest.mark.anyio
async def test_check_with_no_cookie(monkeypatch):
    """A request without a cookie returns {valid: false} (NOT 401)."""
    async with _make_client(monkeypatch) as client:
        resp = await client.get("/api/auth/check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False


@pytest.mark.anyio
async def test_check_when_auth_disabled(monkeypatch):
    """When APP_PASSWORD is unset, check always returns {valid: true}."""
    async with _make_client(monkeypatch, password=None) as client:
        resp = await client.get("/api/auth/check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True


# ── Protected route tests ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_health_works_without_cookie(monkeypatch):
    """Health endpoint is never protected."""
    async with _make_client(monkeypatch) as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


@pytest.mark.anyio
async def test_upload_rejected_without_cookie(monkeypatch):
    """Protected routes return 401 when no cookie is provided."""
    async with _make_client(monkeypatch) as client:
        resp = await client.post("/api/upload")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_upload_accepted_with_valid_cookie(monkeypatch):
    """Protected routes work when a valid cookie is provided (validation
    may fail, but not because of auth)."""
    async with _make_client(monkeypatch) as client:
        resp = await client.post("/api/upload", cookies={"qa_auth": "secret123"})
    # 422 = validation error (no files), but NOT 401
    assert resp.status_code == 422
