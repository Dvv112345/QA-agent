"""Tests for backend/main.py — app factory, health, CORS, exception handler."""

import importlib
import os

import pytest
from httpx import ASGITransport, AsyncClient


def _reload_main(monkeypatch):
    """Reload config → main so env var changes take effect in both modules."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    import backend.config
    import backend.main

    importlib.reload(backend.config)
    importlib.reload(backend.main)


class TestCreateApp:
    """Tests for ``create_app()``."""

    def test_returns_fastapi_instance(self):
        from backend.main import create_app

        app = create_app()
        assert app.title == "QA Agent Backend"

    def test_version_from_config(self, monkeypatch):
        monkeypatch.setenv("VERSION", "2.5.0")
        _reload_main(monkeypatch)
        from backend.main import create_app

        app = create_app()
        assert app.version == "2.5.0"


class TestHealthEndpoint:
    """Tests for GET /api/health."""

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, async_client):
        response = await async_client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_memory_only_storage(self, async_client):
        """When STORE_OFFLINE is false, storage reports memory_only."""
        response = await async_client.get("/api/health")
        assert response.json()["storage"] == "memory_only"

    @pytest.mark.asyncio
    async def test_health_storage_available(self, monkeypatch, tmp_path):
        """With STORE_OFFLINE=true and a writable dir, storage reports available."""
        storage_dir = str(tmp_path / "store")
        monkeypatch.setenv("STORE_OFFLINE", "true")
        monkeypatch.setenv("STORAGE_LOCATION", storage_dir)
        _reload_main(monkeypatch)
        from backend.main import create_app

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["storage"] == "available"

    @pytest.mark.asyncio
    async def test_health_storage_no_location(self, monkeypatch):
        """With STORE_OFFLINE=true but no STORAGE_LOCATION, storage reports unavailable."""
        monkeypatch.setenv("STORE_OFFLINE", "true")
        monkeypatch.delenv("STORAGE_LOCATION", raising=False)
        _reload_main(monkeypatch)
        from backend.main import create_app

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/health")
        assert response.status_code == 200
        assert "unavailable" in response.json()["storage"]


class TestCheckStorageHealth:
    """Tests for ``_check_storage_health()`` directly."""

    def test_memory_only(self, monkeypatch):
        monkeypatch.setenv("STORE_OFFLINE", "false")
        monkeypatch.delenv("STORAGE_LOCATION", raising=False)
        _reload_main(monkeypatch)
        from backend.main import _check_storage_health

        assert _check_storage_health() == "memory_only"

    def test_available(self, monkeypatch, tmp_path):
        storage_dir = str(tmp_path / "store")
        monkeypatch.setenv("STORE_OFFLINE", "true")
        monkeypatch.setenv("STORAGE_LOCATION", storage_dir)
        _reload_main(monkeypatch)
        from backend.main import _check_storage_health

        result = _check_storage_health()
        assert result == "available"
        assert os.path.isdir(storage_dir)

    def test_unavailable_no_location(self, monkeypatch):
        monkeypatch.setenv("STORE_OFFLINE", "true")
        monkeypatch.delenv("STORAGE_LOCATION", raising=False)
        _reload_main(monkeypatch)
        from backend.main import _check_storage_health

        result = _check_storage_health()
        assert "unavailable" in result
        assert "STORAGE_LOCATION" in result


class TestCors:
    """Tests for CORS middleware."""

    @pytest.mark.asyncio
    async def test_cors_headers_present(self, async_client):
        response = await async_client.options(
            "/api/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" in response.headers
        assert "access-control-allow-methods" in response.headers

    @pytest.mark.asyncio
    async def test_cors_disallowed_origin(self, async_client):
        response = await async_client.options(
            "/api/health",
            headers={
                "Origin": "http://evil.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        allow_origin = response.headers.get("access-control-allow-origin", "")
        assert allow_origin != "http://evil.com"


class TestExceptionHandler:
    """Tests for the global exception handler."""

    def test_http_exception_is_reraises(self):
        """The handler re-raises HTTPException so FastAPI handles it normally."""
        from fastapi import HTTPException, Request

        from backend.main import create_app

        app = create_app()

        # Get the handler
        handler = app.exception_handlers[Exception]

        # It should be our custom handler
        exc = HTTPException(status_code=418)
        with pytest.raises(HTTPException):
            # Directly test the re-raise logic: HTTPException → re-raise
            import asyncio

            async def run():
                await handler(Request(scope={"type": "http"}), exc)

            asyncio.run(run())

    def test_other_exception_returns_500(self):
        """Non-HTTP exceptions return a 500 JSONResponse."""
        from fastapi import Request

        from backend.main import create_app

        app = create_app()
        handler = app.exception_handlers[Exception]

        import asyncio

        async def run():
            response = await handler(
                Request(
                    scope={
                        "type": "http",
                        "method": "GET",
                        "path": "/test",
                        "headers": [],
                    }
                ),
                RuntimeError("boom"),
            )
            assert response.status_code == 500
            assert response.body == b'{"detail":"Internal server error"}'

        asyncio.run(run())

    @pytest.mark.asyncio
    async def test_unexpected_error_via_http(self, async_client):
        """A real unexpected server error returns a 500.

        We test this indirectly: the /api/upload endpoint validates filenames
        before our handler runs. Missing file fields produce a 422 via FastAPI's
        own validation. Unexpected errors produce 500. Since we can't easily
        inject a crashing route through the already-created async_client, we
        verify the handler directly in the test above.
        """
        # This test verifies the handler is properly registered on the app
        from backend.main import create_app

        app = create_app()
        assert Exception in app.exception_handlers
