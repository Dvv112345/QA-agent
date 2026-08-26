"""Idempotent schema migrations run at every app startup.

``init_db()`` (``create_all``) only creates *missing tables* — it never
alters existing ones, so every schema change to an existing table ships
here as a migration step (Convention #11).  Each step inspects the live
schema and applies DDL only when it is missing, which keeps every run
idempotent and dialect-portable (PostgreSQL in production, SQLite in
tests).

A step may also be data-only — repairing rows a past bug left behind
(``_settle_orphaned_children``).  Same contract: it must match nothing on
the second run and nothing at all on a fresh database.
"""

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


_SCRIPT_REVISION_COLUMNS = (
    "script_requirement_revision",
    "script_plan_revision",
    "script_env_revision",
)


def _add_test_case_script_revisions(engine: Engine) -> None:
    """Give ``testcase`` the three columns recording what its script was written against.

    ``ALTER TABLE … ADD COLUMN <name> INTEGER`` with no default and no
    constraint is the one form both SQLite and PostgreSQL accept, so the
    step needs no dialect branch.  Existing rows land NULL, which
    ``cicd_eligibility`` reads as *stale* — the script predates the stamp,
    so what it was written against is unknowable.
    """
    inspector = inspect(engine)
    if "testcase" not in inspector.get_table_names():
        return  # fresh database: create_all already made the table with the columns
    existing = {column["name"] for column in inspector.get_columns("testcase")}
    missing = [name for name in _SCRIPT_REVISION_COLUMNS if name not in existing]
    if not missing:
        return
    with engine.begin() as connection:
        for name in missing:
            connection.execute(text(f"ALTER TABLE testcase ADD COLUMN {name} INTEGER"))
    logger.info("Migration: added %s to testcase", ", ".join(missing))


_MIGRATIONS = [
    _add_test_case_script_revisions,
]


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
