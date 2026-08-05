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


# Content-revision counters: three sources and the copies each run takes of
# them. Every column defaults to 0, so existing rows compare equal on both
# sides and no backfill is needed — pre-existing runs read as current.
_CONTENT_REVISION_COLUMNS = (
    ("requirement", ("content_revision",)),
    ("testenvironmentaccess", ("content_revision",)),
    ("testplan", ("content_revision",)),
    ("testexecution", ("requirement_revision", "plan_revision", "env_revision")),
    ("exploratoryrun", ("requirement_revision", "plan_revision", "env_revision")),
)


def _add_content_revisions(engine: Engine) -> None:
    """Add the content-revision counters and their per-run copies when missing."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    for table, column_names in _CONTENT_REVISION_COLUMNS:
        if table not in table_names:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        missing = [name for name in column_names if name not in columns]
        if not missing:
            continue
        with engine.begin() as connection:
            for name in missing:
                connection.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
                )
        logger.info("Migration applied: %s revision columns added (%s)", table, ", ".join(missing))


# Issue-tracker receipt columns, carried identically by both finding
# carriers. Checked per column for the same reason the finding columns
# above are: a half-applied set must not abort the rest.
#
# `issuetrackerconfig` itself needs no migration — it is a new table, and
# create_all builds missing tables.
_TRACKER_TEXT_COLUMNS = (
    "tracker_issue_key",
    "tracker_issue_url",
    "tracker_error",
    "tracker_target",
)
_TRACKER_CARRIERS = ("testcaseexecution", "exploratoryfinding")


def _add_tracker_columns(engine: Engine) -> None:
    """Add the issue-tracker receipt columns to both finding carriers."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    for table in _TRACKER_CARRIERS:
        if table not in table_names:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        missing = [name for name in _TRACKER_TEXT_COLUMNS if name not in columns]
        needs_flag = "tracker_is_duplicate" not in columns
        if not missing and not needs_flag:
            continue
        with engine.begin() as connection:
            for name in missing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} TEXT"))
            if needs_flag:
                connection.execute(
                    text(
                        f"ALTER TABLE {table} ADD COLUMN "
                        "tracker_is_duplicate BOOLEAN NOT NULL DEFAULT FALSE"
                    )
                )
        logger.info(
            "Migration applied: %s tracker columns added (%s)",
            table,
            ", ".join(missing + (["tracker_is_duplicate"] if needs_flag else [])),
        )


def _add_run_export_findings(engine: Engine) -> None:
    """Add ``export_findings`` to both run types when missing.

    Defaults to false, so every run that predates the feature reads as
    "do not file" — which is what it was.
    """
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    for table in ("testrun", "exploratoryrun"):
        if table not in table_names:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "export_findings" in columns:
            continue
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {table} ADD COLUMN export_findings BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        logger.info("Migration applied: %s.export_findings column added", table)


# Orphaned child rows: a case or charter session left `pending`/`running`
# under a parent that already finished. Every writer now settles these
# (services/finalization.py), but databases predating that carry the ones
# produced before it — one backfill, not a recurring repair.
#
# (child table, parent table, foreign key, terminal parent statuses)
_ORPHANED_CHILD_TABLES = (
    ("testcaseexecution", "testexecution", "test_execution_id", ("completed", "failed")),
    ("exploratorysession", "exploratoryrun", "exploratory_run_id", ("completed", "failed")),
)


def _settle_orphaned_children(engine: Engine) -> None:
    """Mark pre-existing children of finished parents ``skipped``.

    Data-only and idempotent: the ``WHERE`` matches nothing on a second
    run, and nothing at all on a fresh database.  Plain SQL with a
    subquery rather than an ORM pass — it is one statement per table and
    runs on every boot.
    """
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    for child, parent, foreign_key, terminal in _ORPHANED_CHILD_TABLES:
        if child not in table_names or parent not in table_names:
            continue
        parent_states = ", ".join(f"'{status}'" for status in terminal)
        with engine.begin() as connection:
            result = connection.execute(
                text(
                    f"UPDATE {child} SET status = 'skipped', "  # noqa: S608 - names are literals above
                    f"error = 'Not run. This run finished before reaching it.' "
                    f"WHERE status IN ('pending', 'running') "
                    f"AND {foreign_key} IN "
                    f"(SELECT id FROM {parent} WHERE status IN ({parent_states}))"
                )
            )
        if result.rowcount:
            logger.info(
                "Migration applied: %d orphaned %s row(s) marked skipped", result.rowcount, child
            )


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
    _add_content_revisions,
    _add_tracker_columns,
    _add_run_export_findings,
    _settle_orphaned_children,
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
