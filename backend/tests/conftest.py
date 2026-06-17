import importlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# Resolve the tests directory for locating sample files
_TESTS_DIR = Path(__file__).resolve().parent


@pytest.fixture
def sample_zip_path() -> Path:
    """Path to a valid sample zip archive for upload tests."""
    return _TESTS_DIR / "sample_zip.zip"


@pytest.fixture
def sample_md_path() -> Path:
    """Path to a valid sample markdown file for upload tests."""
    return _TESTS_DIR / "sample_md.md"


@pytest.fixture
def sample_zip_bytes(sample_zip_path: Path) -> bytes:
    """Bytes of a valid sample zip archive."""
    return sample_zip_path.read_bytes()


@pytest.fixture
def sample_md_bytes(sample_md_path: Path) -> bytes:
    """Bytes of a valid UTF-8 markdown file."""
    return sample_md_path.read_bytes()


@pytest.fixture
async def async_client(monkeypatch):
    """Async HTTP client wired directly to the FastAPI app via ASGI transport.

    Environment is reset to safe defaults before the app is created so that
    tests always start from a known state regardless of what other test
    modules may have reloaded.
    """
    # Prevent load_dotenv from clobbering our test values during reload
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)

    monkeypatch.setenv("STORE_OFFLINE", "false")
    monkeypatch.setenv("MAX_ZIP_FILES", "100000")  # sample zip has many entries
    monkeypatch.delenv("STORAGE_LOCATION", raising=False)

    # Reload config then main so module-level imports pick up our env values
    import backend.config
    import backend.main

    importlib.reload(backend.config)
    importlib.reload(backend.main)

    from backend.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
