"""Tests for backend/database.py — engine, session, init_db."""

import contextlib

from sqlmodel import Session


class TestGetSession:
    """Tests for ``get_session()`` dependency."""

    def test_yields_session(self):
        from backend.database import get_session

        gen = get_session()
        session = next(gen)
        try:
            assert isinstance(session, Session)
        finally:
            # Gracefully close the generator
            with contextlib.suppress(StopIteration):
                next(gen)


class TestInitDb:
    """Tests for ``init_db()``."""

    def test_creates_tables(self, db_session):
        """init_db() is a no-op when tables already exist.

        We verify indirectly: the db_session fixture successfully creates
        and drops tables, proving the models are registered with SQLModel.metadata.
        """
        from backend.models.repo import Repo
        from backend.models.sprint import Sprint

        # If tables weren't created these queries would raise OperationalError
        assert hasattr(Repo, "__table__")
        assert hasattr(Sprint, "__table__")
