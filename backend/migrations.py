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


def _add_testcase_script(engine: Engine) -> None:
    """Add ``testcase.script`` (cached generated test script) when missing."""
    inspector = inspect(engine)
    if "testcase" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("testcase")}
    if "script" in columns:
        return
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE testcase ADD COLUMN script TEXT"))
    logger.info("Migration applied: testcase.script column added")


def _add_test_environment_access_env_vars_json(engine: Engine) -> None:
    """Add ``testenvironmentaccess.env_vars_json`` (extracted access vars) when missing."""
    inspector = inspect(engine)
    if "testenvironmentaccess" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("testenvironmentaccess")}
    if "env_vars_json" in columns:
        return
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE testenvironmentaccess ADD COLUMN env_vars_json TEXT"))
    logger.info("Migration applied: testenvironmentaccess.env_vars_json column added")


def _add_sprint_readme_user_provided(engine: Engine) -> None:
    """Add ``sprint.readme_user_provided`` (README source flag) when missing."""
    inspector = inspect(engine)
    if "sprint" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("sprint")}
    if "readme_user_provided" in columns:
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE sprint ADD COLUMN readme_user_provided BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
    logger.info("Migration applied: sprint.readme_user_provided column added")


def _add_exploratory_finding_environment(engine: Engine) -> None:
    """Add ``exploratoryfinding.environment`` (browser/viewport/OS/URL) when missing."""
    inspector = inspect(engine)
    if "exploratoryfinding" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("exploratoryfinding")}
    if "environment" in columns:
        return
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE exploratoryfinding ADD COLUMN environment TEXT"))
    logger.info("Migration applied: exploratoryfinding.environment column added")


# Structured finding fields on a scripted test-case result, replacing the
# free-text-only report. Checked per column rather than as a group so a run
# interrupted partway through still converges on the next boot.
_TEST_CASE_EXECUTION_FINDING_COLUMNS = (
    "finding_severity",
    "finding_title",
    "finding_steps_to_reproduce",
    "finding_expected",
    "finding_actual",
    "environment",
)


def _add_test_case_execution_finding_fields(engine: Engine) -> None:
    """Add the structured finding columns to ``testcaseexecution`` when missing."""
    inspector = inspect(engine)
    if "testcaseexecution" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("testcaseexecution")}
    missing = [name for name in _TEST_CASE_EXECUTION_FINDING_COLUMNS if name not in columns]
    if not missing:
        return
    with engine.begin() as connection:
        for name in missing:
            connection.execute(text(f"ALTER TABLE testcaseexecution ADD COLUMN {name} TEXT"))
    logger.info(
        "Migration applied: testcaseexecution finding columns added (%s)", ", ".join(missing)
    )


def _add_requirement_archived(engine: Engine) -> None:
    """Add ``requirement.archived`` (soft delete) when missing."""
    inspector = inspect(engine)
    if "requirement" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("requirement")}
    if "archived" in columns:
        return
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE requirement ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE")
        )
    logger.info("Migration applied: requirement.archived column added")


def _add_testcase_archived(engine: Engine) -> None:
    """Add ``testcase.archived`` (superseded by a revision or edit) when missing."""
    inspector = inspect(engine)
    if "testcase" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("testcase")}
    if "archived" in columns:
        return
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE testcase ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE")
        )
    logger.info("Migration applied: testcase.archived column added")


def _relax_testcase_test_plan_id(engine: Engine) -> None:
    """Drop NOT NULL from ``testcase.test_plan_id`` so archived cases can detach.

    The only migration here that is not a plain ``ADD COLUMN``, and the only
    one that has to branch on dialect: PostgreSQL has ``ALTER COLUMN ... DROP
    NOT NULL``, SQLite has no ``ALTER COLUMN`` at all (its workaround is a
    full table rebuild).  Skipping SQLite is correct rather than a
    compromise — the test database is built fresh by ``create_all`` from the
    models, which already declare the column optional, so there is nothing
    to migrate there.

    Consequence worth knowing: this step is only ever exercised against a
    real PostgreSQL database.  The suite can confirm it does not raise, not
    that it works.
    """
    if engine.dialect.name != "postgresql":
        return
    inspector = inspect(engine)
    if "testcase" not in inspector.get_table_names():
        return
    info = next(
        (c for c in inspector.get_columns("testcase") if c["name"] == "test_plan_id"),
        None,
    )
    if info is None or info.get("nullable", True):
        return
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE testcase ALTER COLUMN test_plan_id DROP NOT NULL"))
    logger.info("Migration applied: testcase.test_plan_id NOT NULL dropped")


_MIGRATIONS = [
    _add_requirement_from_prd,
    _add_testcase_script,
    _add_test_environment_access_env_vars_json,
    _add_sprint_readme_user_provided,
    _add_exploratory_finding_environment,
    _add_test_case_execution_finding_fields,
    _add_requirement_archived,
    _add_testcase_archived,
    _relax_testcase_test_plan_id,
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
