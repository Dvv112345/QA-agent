import importlib
from collections.abc import Generator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session, SQLModel, create_engine

_TESTS_DIR = Path(__file__).resolve().parent

TEST_DATABASE_URL = "sqlite:///file:test_db?mode=memory&cache=shared&uri=true"


# ── Globally replace the database engine with SQLite ──────────────────


@pytest.fixture(autouse=True)
def _mock_database_engine(monkeypatch):
    """Replace database._engine with a SQLite engine for every test.

    This runs automatically before every test so no test ever tries to
    connect to a real PostgreSQL server.
    """
    engine = create_engine(TEST_DATABASE_URL, echo=False)

    import backend.database

    monkeypatch.setattr(backend.database, "_engine", engine)

    # Also mock init_db so the app's startup call is a no-op (tables are
    # created by the db_session fixture).
    monkeypatch.setattr(backend.database, "init_db", lambda: None)

    # Set a test encryption key so encrypt_token/decrypt_token work.
    # We generate a fresh key via Fernet.generate_key() rather than
    # hardcoding one so it's always valid.
    from cryptography.fernet import Fernet

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())

    # Prevent dotenv from reading the .env file so that local dev values
    # don't leak into tests.
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)

    # On Windows, SSL_CERT_FILE may point to a non-existent file which
    # breaks httpx.AsyncClient() creation.  Unset it so httpx uses its
    # bundled certificates instead.
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)


# ── Globally isolate tests from Redis ─────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_redis(monkeypatch):
    """Make QueueService connection a no-op so no test touches live Redis.

    With ``_connect`` neutralised the service reports ``available == False``
    and ``enqueue_analysis`` returns ``None``.  Tests that assert enqueue
    behaviour swap in a recording stub instead.
    """
    import backend.services.queue as queue_module

    monkeypatch.setattr(queue_module.QueueService, "_connect", lambda self: None)
    queue_module.reset_queue_service()
    yield
    queue_module.reset_queue_service()


# ── Test database session ────────────────────────────────────────────


@pytest.fixture(scope="function")
def db_session() -> Generator[Session, None, None]:
    """Yield a session bound to a fresh in-memory SQLite database."""
    # Ensure all models are imported before create_all
    from backend.models.database import Repo, Sprint  # noqa: F401

    engine = create_engine(TEST_DATABASE_URL, echo=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    SQLModel.metadata.drop_all(engine)


# ── Sample files ─────────────────────────────────────────────────────


@pytest.fixture
def sample_md_path() -> Path:
    return _TESTS_DIR / "sample_md.md"


@pytest.fixture
def sample_md_bytes(sample_md_path: Path) -> bytes:
    return sample_md_path.read_bytes()


# ── Async HTTP client ────────────────────────────────────────────────


@pytest.fixture
async def async_client(monkeypatch, db_session):
    """Async HTTP client wired to the FastAPI app via ASGI transport."""
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

    async def _override():
        return db_session

    from backend.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = _override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ── Relaxed httpx_mock defaults ──────────────────────────────────────
# Don't fail teardown if mocked responses weren't consumed (tests may
# fail before making all expected HTTP requests).


def pytest_collection_modifyitems(items):
    """Apply relaxed httpx_mock defaults to all async tests."""
    for item in items:
        if "httpx_mock" in item.fixturenames:
            item.add_marker(
                pytest.mark.httpx_mock(
                    assert_all_responses_were_requested=False,
                    assert_all_requests_were_expected=False,
                )
            )


# ── Shared test helpers ────────────────────────────────────────────────


async def _create_repo(client, github_url: str, httpx_mock, description: str = "A repo") -> int:
    """Create a repo via the API and return its database id.

    Registers the necessary ``httpx_mock`` response for the GitHub metadata
    endpoint, then POSTs to ``/api/repos``.
    """
    from backend.utils.github_utils import parse_github_url

    owner, repo_name = parse_github_url(github_url)
    httpx_mock.add_response(
        url=f"https://api.github.com/repos/{owner}/{repo_name}",
        json={"full_name": f"{owner}/{repo_name}", "description": description},
    )
    resp = await client.post("/api/repos", data={"github_url": github_url})
    assert resp.status_code == 201
    return resp.json()["id"]
