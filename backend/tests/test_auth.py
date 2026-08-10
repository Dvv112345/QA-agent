"""Tests for the verify_auth authentication dependency."""

import importlib
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from backend.utils.auth import verify_auth


@pytest.fixture(autouse=True)
def _reload_config(monkeypatch):
    """Reload config before each test so APP_PASSWORD is fresh."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    import backend.config

    importlib.reload(backend.config)


def _make_request(cookie_value: str | None) -> MagicMock:
    """Build a mock FastAPI Request with an optional ``qa_auth`` cookie."""
    request = MagicMock()
    request.cookies = {"qa_auth": cookie_value} if cookie_value is not None else {}
    request.url.path = "/api/upload"
    return request


def test_returns_true_when_app_password_is_unset(monkeypatch):
    """Auth is disabled when APP_PASSWORD is unset or empty."""
    monkeypatch.setenv("APP_PASSWORD", "")
    import backend.config

    importlib.reload(backend.config)

    result = verify_auth(_make_request(None))
    assert result is True


def test_returns_true_when_cookie_matches(monkeypatch):
    """A matching cookie authenticates the request."""
    monkeypatch.setenv("APP_PASSWORD", "secret123")
    import backend.config

    importlib.reload(backend.config)

    result = verify_auth(_make_request("secret123"))
    assert result is True


def test_raises_401_when_no_cookie(monkeypatch):
    """Requests without a cookie are rejected when auth is enabled."""
    monkeypatch.setenv("APP_PASSWORD", "secret123")
    import backend.config

    importlib.reload(backend.config)

    with pytest.raises(HTTPException) as exc_info:
        verify_auth(_make_request(None))
    assert exc_info.value.status_code == 401
    assert "Invalid or missing access code" in exc_info.value.detail


def test_raises_401_when_cookie_mismatch(monkeypatch):
    """A cookie with the wrong value is rejected."""
    monkeypatch.setenv("APP_PASSWORD", "secret123")
    import backend.config

    importlib.reload(backend.config)

    with pytest.raises(HTTPException) as exc_info:
        verify_auth(_make_request("wrong-password"))
    assert exc_info.value.status_code == 401
    assert "Invalid or missing access code" in exc_info.value.detail
