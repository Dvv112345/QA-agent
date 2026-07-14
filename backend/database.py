"""PostgreSQL database engine, session factory, and table initialisation.

Provides the shared SQLAlchemy engine, a FastAPI dependency for obtaining
a per-request session, and ``init_db()`` to create all registered tables
on startup.
"""

import logging
from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

from backend.config import DATABASE_URL

logger = logging.getLogger(__name__)

_engine = create_engine(DATABASE_URL, echo=False)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a per-request database session.

    The session is automatically closed when the request finishes, even if
    an exception is raised.
    """
    with Session(_engine) as session:
        yield session


def new_session() -> Session:
    """Create a standalone session for non-request code (worker, reconciler).

    Reads the module-level engine at call time so that test fixtures which
    replace ``_engine`` also apply to worker code paths.
    """
    return Session(_engine)


def init_db() -> None:
    """Create all tables that do not yet exist in the database.

    Safe to call on every startup — ``create_all`` is a no-op for existing
    tables.
    """
    logger.info("Initialising database tables ...")
    try:
        SQLModel.metadata.create_all(_engine)
        logger.info("Database tables are ready.")
    except Exception:
        logger.exception("Failed to initialise database — is PostgreSQL running?")
        raise
