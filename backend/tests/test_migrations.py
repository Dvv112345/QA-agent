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


def test_skips_database_without_requirement_table():
    """A brand-new database (before create_all) must not raise."""
    engine = create_engine("sqlite://")
    run_migrations(engine)


def test_reads_module_engine_at_call_time(db_session):
    """No-arg call resolves the (test-patched) module engine, like new_session."""
    run_migrations()
