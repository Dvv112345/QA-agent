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


def test_noop_on_fresh_schema(db_session):
    """A schema built by create_all already has every column — twice is safe."""
    engine = db_session.get_bind()
    run_migrations(engine)
    run_migrations(engine)
    assert "from_prd" in _requirement_columns(engine)
    assert "script" in _testcase_columns(engine)
    assert "env_vars_json" in _test_environment_access_columns(engine)
    assert "readme_user_provided" in _sprint_columns(engine)


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


def test_skips_database_without_requirement_table():
    """A brand-new database (before create_all) must not raise."""
    engine = create_engine("sqlite://")
    run_migrations(engine)


def test_reads_module_engine_at_call_time(db_session):
    """No-arg call resolves the (test-patched) module engine, like new_session."""
    run_migrations()
