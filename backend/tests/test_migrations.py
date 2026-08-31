"""Startup migrations must be idempotent and dialect-portable."""

from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine

from backend.migrations import _SCRIPT_REVISION_COLUMNS, run_migrations
from backend.tests.conftest import TEST_CONNECT_ARGS


def _fresh_engine(tmp_path):
    # Import the models for their side effect: `create_all` builds whatever
    # is registered on the metadata, and nothing else in this file's import
    # chain touches them — so run alone, the file used to create an empty
    # database and fail on a table it had never made.
    import backend.models.database  # noqa: F401

    engine = create_engine(f"sqlite:///{tmp_path / 'mig.db'}", connect_args=TEST_CONNECT_ARGS)
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_sprint_row(engine) -> None:
    """A repo and a sprint, so a defectgroup row satisfies its foreign key.

    Through the ORM rather than raw SQL: the tables carry NOT NULL columns
    with Python-side defaults, which a hand-written INSERT has to restate
    and then keep in step forever.
    """
    from sqlmodel import Session

    from backend.models.database import Repo, Sprint

    with Session(engine) as session:
        session.add(Repo(github_link="https://github.com/x/y", name="x/y"))
        session.commit()
        session.add(Sprint(name="S", repo_id=1, directory="d"))
        session.commit()


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


def test_run_migrations_adds_and_backfills_the_defect_group_pool(tmp_path):
    """A defectgroup table predating pools gains the column, non-NULL.

    The backfill is the half that matters: grouping filters the known
    defects by pool, so a NULL row drops out of every match and re-opens a
    defect that already exists.
    """
    engine = _fresh_engine(tmp_path)
    _seed_sprint_row(engine)
    with engine.begin() as connection:
        # SQLite refuses to drop an indexed column, so the index goes first —
        # which is also why the migration recreates it after the ALTER.
        connection.execute(text("DROP INDEX IF EXISTS ix_defectgroup_pool"))
        connection.execute(text("ALTER TABLE defectgroup DROP COLUMN pool"))
        connection.execute(
            text(
                "INSERT INTO defectgroup (sprint_id, title, expected, actual, created_at) "
                "VALUES (1, 'T', 'E', 'A', CURRENT_TIMESTAMP)"
            )
        )
    assert "pool" not in _columns(engine, "defectgroup")

    run_migrations(engine)

    assert "pool" in _columns(engine, "defectgroup")
    indexes = {index["name"] for index in inspect(engine).get_indexes("defectgroup")}
    assert "ix_defectgroup_pool" in indexes
    with engine.begin() as connection:
        pools = [row[0] for row in connection.execute(text("SELECT pool FROM defectgroup"))]
    assert pools == ["functional"]


def test_the_pool_backfill_leaves_existing_values_alone(tmp_path):
    """Only NULL rows are touched — a nonfunctional group stays put."""
    engine = _fresh_engine(tmp_path)
    _seed_sprint_row(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO defectgroup "
                "(sprint_id, pool, title, expected, actual, created_at) "
                "VALUES (1, 'nonfunctional', 'T', 'E', 'A', CURRENT_TIMESTAMP)"
            )
        )

    run_migrations(engine)
    run_migrations(engine)

    with engine.begin() as connection:
        pools = [row[0] for row in connection.execute(text("SELECT pool FROM defectgroup"))]
    assert pools == ["nonfunctional"]
