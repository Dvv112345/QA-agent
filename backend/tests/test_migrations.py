"""Startup migrations must be idempotent and dialect-portable."""

from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine

from backend.migrations import _SCRIPT_REVISION_COLUMNS, run_migrations
from backend.tests.conftest import TEST_CONNECT_ARGS


def _fresh_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mig.db'}", connect_args=TEST_CONNECT_ARGS)
    SQLModel.metadata.create_all(engine)
    return engine


def _columns(engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def test_run_migrations_is_idempotent(tmp_path):
    engine = _fresh_engine(tmp_path)

    run_migrations(engine)
    run_migrations(engine)  # second pass must match nothing and raise nothing

    assert set(_SCRIPT_REVISION_COLUMNS) <= _columns(engine, "testcase")


def test_run_migrations_adds_missing_script_revision_columns(tmp_path):
    """A `testcase` table predating the stamp gains all three columns."""
    engine = _fresh_engine(tmp_path)
    with engine.begin() as connection:
        for name in _SCRIPT_REVISION_COLUMNS:
            connection.execute(text(f"ALTER TABLE testcase DROP COLUMN {name}"))
    assert not (set(_SCRIPT_REVISION_COLUMNS) & _columns(engine, "testcase"))

    run_migrations(engine)

    assert set(_SCRIPT_REVISION_COLUMNS) <= _columns(engine, "testcase")


def test_run_migrations_on_an_empty_database_is_a_no_op(tmp_path):
    """No `testcase` table at all — the step returns rather than raising."""
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", connect_args=TEST_CONNECT_ARGS)

    run_migrations(engine)

    assert "testcase" not in inspect(engine).get_table_names()
