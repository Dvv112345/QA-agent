"""Tests for backend/routes/upload.py — POST /api/upload endpoint."""

import importlib
import io
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient


def _make_zip(files: dict) -> bytes:
    """Create an in-memory zip from a dict of {path: content}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def _upload_files(zip_bytes, zip_filename, md_bytes, md_filename):
    """Build multipart form fields for the upload endpoint."""
    return {
        "zip_file": (zip_filename, zip_bytes, "application/zip"),
        "markdown_file": (md_filename, md_bytes, "text/markdown"),
    }


def _get_detail(response) -> str:
    """Extract a flat string from a response detail, which may be a
    string or a list of error dicts (FastAPI validation errors)."""
    detail = response.json()["detail"]
    if isinstance(detail, list):
        return " ".join(str(d.get("msg", d)) for d in detail)
    return str(detail)


class TestUploadSuccess:
    """Happy-path tests for POST /api/upload."""

    @pytest.mark.asyncio
    async def test_valid_upload(self, async_client, sample_zip_bytes, sample_md_bytes):
        files = _upload_files(sample_zip_bytes, "test.zip", sample_md_bytes, "test.md")
        response = await async_client.post("/api/upload", files=files)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "received"
        assert data["zip_filename"] == "test.zip"
        assert data["markdown_filename"] == "test.md"
        assert "job_id" in data
        assert isinstance(data["tree"], list)
        assert isinstance(data["tree_text"], str)
        assert data["error"] is None

    @pytest.mark.asyncio
    async def test_markdown_extension_accepted(self, async_client, sample_md_bytes):
        """.markdown extension is accepted alongside .md."""
        zip_bytes = _make_zip({"a.py": "print(1)"})
        files = _upload_files(zip_bytes, "code.zip", sample_md_bytes, "spec.markdown")
        response = await async_client.post("/api/upload", files=files)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_small_zip(self, async_client, sample_md_bytes):
        """A minimal valid zip should succeed."""
        zip_bytes = _make_zip({"readme.txt": "hello"})
        files = _upload_files(zip_bytes, "small.zip", sample_md_bytes, "docs.md")
        response = await async_client.post("/api/upload", files=files)
        assert response.status_code == 200


class TestUploadValidation:
    """Validation and error-handling tests for POST /api/upload."""

    @pytest.mark.asyncio
    async def test_missing_zip_filename(self, async_client, sample_md_bytes):
        """Submitting a file field without a filename is rejected by FastAPI
        before our route handler runs (the field is received as a string, not
        an UploadFile)."""
        response = await async_client.post(
            "/api/upload",
            data={
                "zip_file": "not-a-file",
                "markdown_file": ("ok.md", sample_md_bytes, "text/markdown"),
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_md_filename(self, async_client, sample_zip_bytes):
        response = await async_client.post(
            "/api/upload",
            data={
                "zip_file": ("ok.zip", sample_zip_bytes, "application/zip"),
                "markdown_file": "not-a-file",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_zip_extension(self, async_client, sample_md_bytes):
        response = await async_client.post(
            "/api/upload",
            files={
                "zip_file": ("code.rar", b"data", "application/x-rar-compressed"),
                "markdown_file": ("ok.md", sample_md_bytes, "text/markdown"),
            },
        )
        assert response.status_code == 422
        assert "zip" in _get_detail(response).lower()

    @pytest.mark.asyncio
    async def test_invalid_md_extension(self, async_client, sample_zip_bytes):
        response = await async_client.post(
            "/api/upload",
            files={
                "zip_file": ("ok.zip", sample_zip_bytes, "application/zip"),
                "markdown_file": ("notes.txt", b"text", "text/plain"),
            },
        )
        assert response.status_code == 422
        assert "markdown" in _get_detail(response).lower()

    @pytest.mark.asyncio
    async def test_invalid_zip_magic_bytes(self, async_client, sample_md_bytes):
        """Content that isn't a real zip is rejected."""
        response = await async_client.post(
            "/api/upload",
            files={
                "zip_file": ("fake.zip", b"not a zip file", "application/zip"),
                "markdown_file": ("ok.md", sample_md_bytes, "text/markdown"),
            },
        )
        assert response.status_code == 422
        assert "zip" in _get_detail(response).lower()

    @pytest.mark.asyncio
    async def test_non_utf8_markdown(self, async_client, sample_zip_bytes):
        """Non-UTF-8 markdown bytes are rejected."""
        non_utf8 = bytes([0xFF, 0xFE, 0x00, 0x00])
        response = await async_client.post(
            "/api/upload",
            files={
                "zip_file": ("ok.zip", sample_zip_bytes, "application/zip"),
                "markdown_file": ("bad.md", non_utf8, "text/markdown"),
            },
        )
        assert response.status_code == 422
        detail = _get_detail(response).lower()
        assert "utf-8" in detail or "utf" in detail

    @pytest.mark.asyncio
    async def test_oversized_zip(self, monkeypatch, sample_md_bytes):
        """Zip exceeding MAX_UPLOAD_SIZE_MB is rejected."""
        # Force a 0 MB limit and recreate app + routes
        monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "0")

        import backend.config
        import backend.routes.upload

        importlib.reload(backend.config)
        importlib.reload(backend.routes.upload)

        from backend.main import create_app

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/upload",
                files={
                    "zip_file": ("big.zip", b"PK\x03\x04" + b"x" * 100, "application/zip"),
                    "markdown_file": ("ok.md", sample_md_bytes, "text/markdown"),
                },
            )
        assert response.status_code == 422
        assert "size" in _get_detail(response).lower()

    @pytest.mark.asyncio
    async def test_oversized_markdown(self, monkeypatch, sample_zip_bytes):
        """Markdown exceeding MAX_UPLOAD_SIZE_MB is rejected."""
        monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "0")

        import backend.config
        import backend.routes.upload

        importlib.reload(backend.config)
        importlib.reload(backend.routes.upload)

        from backend.main import create_app

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/upload",
                files={
                    "zip_file": ("ok.zip", sample_zip_bytes, "application/zip"),
                    "markdown_file": ("big.md", b"x" * 100, "text/markdown"),
                },
            )
        assert response.status_code == 422
        assert "size" in _get_detail(response).lower()

    @pytest.mark.asyncio
    async def test_corrupt_zip(self, async_client, sample_md_bytes):
        """Valid magic bytes but broken zip structure returns 422."""
        corrupt = b"PK\x03\x04" + b"corrupted content here"
        response = await async_client.post(
            "/api/upload",
            files={
                "zip_file": ("bad.zip", corrupt, "application/zip"),
                "markdown_file": ("ok.md", sample_md_bytes, "text/markdown"),
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_zip_with_path_traversal(self, async_client, sample_md_bytes):
        """Zip with ``../`` entries is rejected."""
        zip_bytes = _make_zip({"../escape.txt": "bad"})
        response = await async_client.post(
            "/api/upload",
            files={
                "zip_file": ("evil.zip", zip_bytes, "application/zip"),
                "markdown_file": ("ok.md", sample_md_bytes, "text/markdown"),
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_zip_with_absolute_path(self, async_client, sample_md_bytes):
        """Zip with absolute path entries is rejected."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("/etc/passwd", "malicious")
        response = await async_client.post(
            "/api/upload",
            files={
                "zip_file": ("evil.zip", buf.getvalue(), "application/zip"),
                "markdown_file": ("ok.md", sample_md_bytes, "text/markdown"),
            },
        )
        assert response.status_code == 422
