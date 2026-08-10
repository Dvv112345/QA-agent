import importlib
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

_TESTS_DIR = Path(__file__).resolve().parent

TEST_DATABASE_URL = "sqlite:///file:test_db?mode=memory&cache=shared&uri=true"

# sqlite3 refuses a connection used from a thread other than the one that
# opened it.  Nothing in production hits that — psycopg2 has no such
# guard — but a route that offloads database work with `asyncio.to_thread`
# (the export-findings retry routes) genuinely runs it on another thread,
# so without this the whole shape is untestable and every test of one has
# to stub the work out.  Sequential use is still the only use: the routes
# await the thread, so there is never more than one toucher at a time.
TEST_CONNECT_ARGS = {"check_same_thread": False}


# ── Enforce foreign keys on every SQLite connection ───────────────────
# SQLite ignores foreign keys unless asked, so without this a test can
# orphan a child row (or delete a parent out from under one) and still
# pass, while PostgreSQL rejects the same statement in production.  The
# listener is registered on the Engine *class* rather than an instance
# because engines are built in more than one place: both fixtures below,
# plus any ad-hoc engine a test builds for itself.


@event.listens_for(Engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# ── Globally replace the database engine with SQLite ──────────────────


@pytest.fixture(autouse=True)
def _mock_database_engine(monkeypatch):
    """Replace database._engine with a SQLite engine for every test.

    This runs automatically before every test so no test ever tries to
    connect to a real PostgreSQL server.
    """
    engine = create_engine(TEST_DATABASE_URL, echo=False, connect_args=TEST_CONNECT_ARGS)

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

    engine = create_engine(TEST_DATABASE_URL, echo=False, connect_args=TEST_CONNECT_ARGS)
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
async def async_client(monkeypatch, db_session, tmp_path):
    """Async HTTP client wired to the FastAPI app via ASGI transport."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.setenv("STORE_OFFLINE", "false")
    # Sprint creation always makes a real directory (generate_sprint_directory
    # is unconditional, independent of STORE_OFFLINE) — point it at tmp_path
    # so pytest cleans it up instead of leaking UUID dirs into the repo root.
    monkeypatch.setenv("STORAGE_LOCATION", str(tmp_path))
    monkeypatch.delenv("APP_PASSWORD", raising=False)

    import backend.config
    import backend.main

    importlib.reload(backend.config)
    importlib.reload(backend.main)

    monkeypatch.setattr(backend.main, "_check_redis_health", lambda: "mocked")

    # backend.routes.sprints and backend.services.storage each imported
    # STORE_OFFLINE/STORAGE_LOCATION by value at module import time (same
    # pattern as backend.utils.readme_utils, patched ad hoc elsewhere) —
    # reloading backend.config above never reaches those already-bound
    # names. Left unpatched, they keep whatever the real backend/.env had
    # at first import (often STORE_OFFLINE=true for local dev), so every
    # sprint-creating test silently writes a real README.md/PRD file under
    # the repo's own ./uploads/<uuid>/ instead of a throwaway temp dir.
    # generate_sprint_directory() also runs unconditionally regardless of
    # STORE_OFFLINE, so its directory needs the same treatment. Patch all
    # three names directly so every test writes inside tmp_path instead.
    import backend.routes.sprints as sprints_routes
    import backend.services.storage as storage_module

    monkeypatch.setattr(sprints_routes, "STORAGE_LOCATION", str(tmp_path))
    monkeypatch.setattr(storage_module, "STORE_OFFLINE", False)
    monkeypatch.setattr(storage_module, "STORAGE_LOCATION", str(tmp_path))

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
