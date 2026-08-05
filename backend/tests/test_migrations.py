"""Tests for the idempotent startup migrations."""

from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine

from backend.migrations import run_migrations


def _requirement_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("requirement")}


def _testcase_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("testcase")}


def _test_environment_access_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("testenvironmentaccess")}


def _sprint_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("sprint")}


def _exploratory_finding_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("exploratoryfinding")}


def _test_case_execution_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("testcaseexecution")}


_FINDING_COLUMNS = {
    "finding_severity",
    "finding_title",
    "finding_steps_to_reproduce",
    "finding_expected",
    "finding_actual",
    "environment",
}

_TRACKER_COLUMNS = {
    "tracker_issue_key",
    "tracker_issue_url",
    "tracker_error",
    "tracker_target",
    "tracker_is_duplicate",
}


def _test_run_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("testrun")}


def _exploratory_run_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("exploratoryrun")}


def test_noop_on_fresh_schema(db_session):
    """A schema built by create_all already has every column — twice is safe."""
    engine = db_session.get_bind()
    run_migrations(engine)
    run_migrations(engine)
    assert "from_prd" in _requirement_columns(engine)
    assert "script" in _testcase_columns(engine)
    assert "env_vars_json" in _test_environment_access_columns(engine)
    assert "readme_user_provided" in _sprint_columns(engine)
    assert "environment" in _exploratory_finding_columns(engine)
    assert _test_case_execution_columns(engine) >= _FINDING_COLUMNS
    assert "archived" in _requirement_columns(engine)
    assert "archived" in _testcase_columns(engine)
    assert "content_revision" in _requirement_columns(engine)
    assert "content_revision" in _test_environment_access_columns(engine)
    assert _test_case_execution_columns(engine) >= _TRACKER_COLUMNS
    assert _exploratory_finding_columns(engine) >= _TRACKER_COLUMNS
    assert "export_findings" in _test_run_columns(engine)
    assert "export_findings" in _exploratory_run_columns(engine)


def _test_execution_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("testexecution")}


_RUN_REVISION_COLUMNS = {"requirement_revision", "plan_revision", "env_revision"}


def test_adds_missing_content_revision_columns():
    """A database predating the counters gets them, defaulting to 0.

    The default is what makes existing runs read as current without any
    backfill: both sides of every comparison start at 0.
    """
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE requirement DROP COLUMN content_revision"))
        for name in _RUN_REVISION_COLUMNS:
            connection.execute(text(f"ALTER TABLE testexecution DROP COLUMN {name}"))
    assert "content_revision" not in _requirement_columns(engine)

    run_migrations(engine)
    assert "content_revision" in _requirement_columns(engine)
    assert _test_execution_columns(engine) >= _RUN_REVISION_COLUMNS
    run_migrations(engine)  # idempotent on the migrated schema too
    assert _test_execution_columns(engine) >= _RUN_REVISION_COLUMNS


def test_adds_missing_requirement_archived_column():
    """An existing database predating the soft delete gets the column once."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE requirement DROP COLUMN archived"))
    assert "archived" not in _requirement_columns(engine)

    run_migrations(engine)
    assert "archived" in _requirement_columns(engine)
    run_migrations(engine)  # idempotent on the migrated schema too
    assert "archived" in _requirement_columns(engine)


def test_adds_missing_testcase_archived_column():
    """An existing database predating case archiving gets the column once."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE testcase DROP COLUMN archived"))
    assert "archived" not in _testcase_columns(engine)

    run_migrations(engine)
    assert "archived" in _testcase_columns(engine)
    run_migrations(engine)  # idempotent on the migrated schema too
    assert "archived" in _testcase_columns(engine)


def test_test_plan_id_nullability_step_skips_sqlite():
    """The one non-ADD-COLUMN migration must no-op rather than raise here.

    SQLite has no ``ALTER COLUMN``, and its schema is built fresh from the
    models (which already declare ``test_plan_id`` optional), so there is
    nothing to do.  This asserts the skip, not the DDL — the PostgreSQL
    branch is only reachable against a real database.
    """
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)

    run_migrations(engine)  # must not raise

    info = next(c for c in inspect(engine).get_columns("testcase") if c["name"] == "test_plan_id")
    assert info["nullable"] is True


def test_adds_missing_testcase_script_column():
    """An existing database predating the column gets it added exactly once."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE testcase DROP COLUMN script"))
    assert "script" not in _testcase_columns(engine)

    run_migrations(engine)
    assert "script" in _testcase_columns(engine)
    run_migrations(engine)  # idempotent on the migrated schema too
    assert "script" in _testcase_columns(engine)


def test_adds_missing_env_vars_json_column():
    """An existing database predating the column gets it added exactly once."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE testenvironmentaccess DROP COLUMN env_vars_json"))
    assert "env_vars_json" not in _test_environment_access_columns(engine)

    run_migrations(engine)
    assert "env_vars_json" in _test_environment_access_columns(engine)
    run_migrations(engine)  # idempotent on the migrated schema too
    assert "env_vars_json" in _test_environment_access_columns(engine)


def test_adds_missing_from_prd_column():
    """An existing database predating the column gets it added exactly once."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE requirement DROP COLUMN from_prd"))
    assert "from_prd" not in _requirement_columns(engine)

    run_migrations(engine)
    assert "from_prd" in _requirement_columns(engine)
    run_migrations(engine)  # idempotent on the migrated schema too
    assert "from_prd" in _requirement_columns(engine)


def test_adds_missing_readme_user_provided_column():
    """An existing database predating the column gets it added exactly once."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE sprint DROP COLUMN readme_user_provided"))
    assert "readme_user_provided" not in _sprint_columns(engine)

    run_migrations(engine)
    assert "readme_user_provided" in _sprint_columns(engine)
    run_migrations(engine)  # idempotent on the migrated schema too
    assert "readme_user_provided" in _sprint_columns(engine)


def test_adds_missing_exploratory_finding_environment_column():
    """An existing database predating the column gets it added exactly once."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE exploratoryfinding DROP COLUMN environment"))
    assert "environment" not in _exploratory_finding_columns(engine)

    run_migrations(engine)
    assert "environment" in _exploratory_finding_columns(engine)
    run_migrations(engine)  # idempotent on the migrated schema too
    assert "environment" in _exploratory_finding_columns(engine)


def test_adds_missing_test_case_execution_finding_columns():
    """An existing database predating the structured finding gets all six."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        for name in _FINDING_COLUMNS:
            connection.execute(text(f"ALTER TABLE testcaseexecution DROP COLUMN {name}"))
    assert not (_FINDING_COLUMNS & _test_case_execution_columns(engine))

    run_migrations(engine)
    assert _test_case_execution_columns(engine) >= _FINDING_COLUMNS
    run_migrations(engine)  # idempotent on the migrated schema too
    assert _test_case_execution_columns(engine) >= _FINDING_COLUMNS


def test_converges_on_partially_migrated_test_case_execution():
    """A run interrupted midway leaves some columns — the next boot finishes.

    Each column is checked individually for exactly this case; a group
    check would see one present and skip the rest forever.
    """
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        for name in ("finding_expected", "finding_actual", "environment"):
            connection.execute(text(f"ALTER TABLE testcaseexecution DROP COLUMN {name}"))

    run_migrations(engine)
    assert _test_case_execution_columns(engine) >= _FINDING_COLUMNS


def test_adds_missing_tracker_columns_to_both_carriers():
    """A database predating the integration gets the receipt columns on both.

    Both finding carriers are migrated by one parametrized step, so this
    asserts them together — a step that reached only the scripted side
    would leave exploratory findings unable to record where they were filed.
    """
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        for table in ("testcaseexecution", "exploratoryfinding"):
            for name in _TRACKER_COLUMNS:
                connection.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))
    assert not (_TRACKER_COLUMNS & _test_case_execution_columns(engine))
    assert not (_TRACKER_COLUMNS & _exploratory_finding_columns(engine))

    run_migrations(engine)
    assert _test_case_execution_columns(engine) >= _TRACKER_COLUMNS
    assert _exploratory_finding_columns(engine) >= _TRACKER_COLUMNS
    run_migrations(engine)  # idempotent on the migrated schema too
    assert _test_case_execution_columns(engine) >= _TRACKER_COLUMNS


def test_converges_on_partially_migrated_tracker_columns():
    """A run interrupted midway leaves some columns — the next boot finishes.

    Includes the boolean specifically: it is added by a separate branch
    from the four TEXT columns, so a check that only counted the latter
    would skip it forever once they were present.
    """
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        for name in ("tracker_error", "tracker_target", "tracker_is_duplicate"):
            connection.execute(text(f"ALTER TABLE testcaseexecution DROP COLUMN {name}"))
        connection.execute(text("ALTER TABLE exploratoryfinding DROP COLUMN tracker_is_duplicate"))

    run_migrations(engine)
    assert _test_case_execution_columns(engine) >= _TRACKER_COLUMNS
    assert _exploratory_finding_columns(engine) >= _TRACKER_COLUMNS


def test_adds_missing_export_findings_to_both_run_types():
    """Existing runs get the flag defaulting to false — which is what they were."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        for table in ("testrun", "exploratoryrun"):
            connection.execute(text(f"ALTER TABLE {table} DROP COLUMN export_findings"))
    assert "export_findings" not in _test_run_columns(engine)
    assert "export_findings" not in _exploratory_run_columns(engine)

    run_migrations(engine)
    assert "export_findings" in _test_run_columns(engine)
    assert "export_findings" in _exploratory_run_columns(engine)
    run_migrations(engine)  # idempotent on the migrated schema too
    assert "export_findings" in _test_run_columns(engine)


def test_converges_when_only_one_run_type_was_migrated():
    """Each table is checked on its own, so a half-applied pair converges."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE exploratoryrun DROP COLUMN export_findings"))

    run_migrations(engine)
    assert "export_findings" in _exploratory_run_columns(engine)
    assert "export_findings" in _test_run_columns(engine)


def _seed_orphan_case(db_session, *, parent_status, case_status):
    """A TestExecution in *parent_status* holding one case in *case_status*.

    Seeded through the models (and the shared fixture engine, so the
    ``PRAGMA foreign_keys=ON`` in conftest applies) and then forced into
    the combination a pre-fix database ended up with, which no writer
    produces any more.
    """
    from backend.models.database import RequirementStatus
    from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
    from backend.tests.test_sprints import (
        _seed_test_case,
        _seed_test_case_execution,
        _seed_test_execution,
        _seed_test_plan,
        _seed_test_run,
    )

    sprint = _seed_sprint(db_session)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    plan = _seed_test_plan(db_session, requirement)
    run = _seed_test_run(db_session, sprint)
    execution = _seed_test_execution(db_session, run, requirement, status=parent_status)
    case_execution = _seed_test_case_execution(
        db_session, execution, _seed_test_case(db_session, plan), status=case_status
    )
    return case_execution.id


def _case_status(db_session, case_execution_id):
    from backend.models.database import TestCaseExecution

    db_session.expire_all()
    return db_session.get(TestCaseExecution, case_execution_id).status


def test_settles_children_orphaned_by_a_finished_parent(db_session):
    """Rows stranded before the fix are repaired once, on the next boot.

    Every writer settles these now, but a database that already ran the
    buggy code carries the orphans it produced — and the commonest cause
    (a superseded run) can never be restarted, so nothing else would ever
    revisit them.
    """
    case_id = _seed_orphan_case(db_session, parent_status="failed", case_status="running")

    run_migrations(db_session.get_bind())
    assert _case_status(db_session, case_id) == "skipped"
    run_migrations(db_session.get_bind())  # idempotent — nothing left to match
    assert _case_status(db_session, case_id) == "skipped"


def test_leaves_children_of_a_live_parent_alone(db_session):
    """A run still in progress is not an orphan — its cases are just queued."""
    case_id = _seed_orphan_case(db_session, parent_status="running", case_status="pending")

    run_migrations(db_session.get_bind())
    assert _case_status(db_session, case_id) == "pending"


def test_skips_database_without_requirement_table():
    """A brand-new database (before create_all) must not raise."""
    engine = create_engine("sqlite://")
    run_migrations(engine)


def test_reads_module_engine_at_call_time(db_session):
    """No-arg call resolves the (test-patched) module engine, like new_session."""
    run_migrations()
