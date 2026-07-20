"""Idempotent schema migrations run at every app startup.

``init_db()`` (``create_all``) only creates *missing tables* — it never
alters existing ones, so every schema change to an existing table ships
here as a migration step (Convention #11).  Each step inspects the live
schema and applies DDL only when it is missing, which keeps every run
idempotent and dialect-portable (PostgreSQL in production, SQLite in
tests).
"""

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def _add_requirement_from_prd(engine: Engine) -> None:
    """Add ``requirement.from_prd`` (marks PRD-derived rows) when missing."""
    inspector = inspect(engine)
    if "requirement" not in inspector.get_table_names():
        # Fresh database — create_all builds the full table, column included.
        return
    columns = {column["name"] for column in inspector.get_columns("requirement")}
    if "from_prd" in columns:
        return
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE requirement ADD COLUMN from_prd BOOLEAN NOT NULL DEFAULT FALSE")
        )
    logger.info("Migration applied: requirement.from_prd column added")


_MIGRATIONS = [_add_requirement_from_prd]


def run_migrations(engine: Engine | None = None) -> None:
    """Apply all pending migrations; call after ``init_db()`` at startup.

    Reads the module-level engine at call time (like ``new_session``) so
    test fixtures that replace ``backend.database._engine`` also apply here.
    """
    if engine is None:
        import backend.database

        engine = backend.database._engine
    for migration in _MIGRATIONS:
        migration(engine)
