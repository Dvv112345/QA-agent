"""Tests for the idempotent startup migrations."""

from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine

from backend.migrations import run_migrations


def _requirement_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("requirement")}


def test_noop_on_fresh_schema(db_session):
    """A schema built by create_all already has every column — twice is safe."""
    engine = db_session.get_bind()
    run_migrations(engine)
    run_migrations(engine)
    assert "from_prd" in _requirement_columns(engine)


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


def test_skips_database_without_requirement_table():
    """A brand-new database (before create_all) must not raise."""
    engine = create_engine("sqlite://")
    run_migrations(engine)


def test_reads_module_engine_at_call_time(db_session):
    """No-arg call resolves the (test-patched) module engine, like new_session."""
    run_migrations()
