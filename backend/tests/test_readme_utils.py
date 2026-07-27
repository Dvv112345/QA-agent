"""Tests for backend/utils/readme_utils.py — README/file-tree refresh helpers."""

import os

import pytest

from backend.services import storage as storage_module
from backend.tests.test_requirement_routes import _seed_sprint
from backend.utils import readme_utils


def _write_stored_readme(sprint, storage_location, content: str) -> str:
    sprint_dir = os.path.join(storage_location, sprint.directory)
    os.makedirs(sprint_dir, exist_ok=True)
    path = os.path.join(sprint_dir, "README.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


@pytest.fixture
def offline_storage(monkeypatch, tmp_path):
    """Point readme_utils' and storage's module-level config constants at a
    temp dir — each imported STORE_OFFLINE/STORAGE_LOCATION by value at
    import time, so both need patching independently (see conftest.py's
    ``async_client`` fixture for the same gotcha)."""
    monkeypatch.setattr(readme_utils, "STORE_OFFLINE", True)
    monkeypatch.setattr(readme_utils, "STORAGE_LOCATION", str(tmp_path))
    monkeypatch.setattr(storage_module, "STORE_OFFLINE", True)
    monkeypatch.setattr(storage_module, "STORAGE_LOCATION", str(tmp_path))
    return str(tmp_path)


# ── resolve_readme(force_refresh=True) ─────────────────────────────────


class TestForceRefreshReadme:
    @pytest.mark.asyncio
    async def test_success_overwrites_stored_copy(self, db_session, offline_storage, monkeypatch):
        sprint = _seed_sprint(db_session)
        path = _write_stored_readme(sprint, offline_storage, "# Old README")

        async def _fresh(*args, **kwargs):
            return "# Fresh README"

        monkeypatch.setattr(readme_utils, "download_readme", _fresh)

        result = await readme_utils.resolve_readme(sprint, force_refresh=True)

        assert result == "# Fresh README"
        with open(path, encoding="utf-8") as fh:
            assert fh.read() == "# Fresh README"

    @pytest.mark.asyncio
    async def test_github_failure_falls_back_to_stored_copy(
        self, db_session, offline_storage, monkeypatch
    ):
        sprint = _seed_sprint(db_session)
        _write_stored_readme(sprint, offline_storage, "# Old README")

        async def _boom(*args, **kwargs):
            raise RuntimeError("GitHub unreachable")

        monkeypatch.setattr(readme_utils, "download_readme", _boom)

        result = await readme_utils.resolve_readme(sprint, force_refresh=True)

        assert result == "# Old README"

    @pytest.mark.asyncio
    async def test_github_failure_with_no_stored_copy_returns_none(
        self, db_session, offline_storage, monkeypatch
    ):
        sprint = _seed_sprint(db_session)

        async def _boom(*args, **kwargs):
            raise RuntimeError("GitHub unreachable")

        monkeypatch.setattr(readme_utils, "download_readme", _boom)

        result = await readme_utils.resolve_readme(sprint, force_refresh=True)

        assert result is None

    @pytest.mark.asyncio
    async def test_force_refresh_false_prefers_stored_copy_without_downloading(
        self, db_session, offline_storage, monkeypatch
    ):
        sprint = _seed_sprint(db_session)
        _write_stored_readme(sprint, offline_storage, "# Old README")

        called = False

        async def _fresh(*args, **kwargs):
            nonlocal called
            called = True
            return "# Fresh README"

        monkeypatch.setattr(readme_utils, "download_readme", _fresh)

        result = await readme_utils.resolve_readme(sprint)

        assert result == "# Old README"
        assert called is False


# ── refresh_file_tree ────────────────────────────────────────────────


class TestRefreshFileTree:
    @pytest.mark.asyncio
    async def test_success_updates_repo_file_tree(self, db_session, monkeypatch):
        sprint = _seed_sprint(db_session)
        sprint.repo.file_tree = "old.py"

        async def _metadata(*args, **kwargs):
            return {"default_branch": "main"}

        async def _tree(*args, **kwargs):
            return "src/app.py\nsrc/db.py"

        monkeypatch.setattr(readme_utils, "fetch_repo_metadata", _metadata)
        monkeypatch.setattr(readme_utils, "fetch_file_tree", _tree)

        result = await readme_utils.refresh_file_tree(sprint)

        assert result == "src/app.py\nsrc/db.py"
        assert sprint.repo.file_tree == "src/app.py\nsrc/db.py"

    @pytest.mark.asyncio
    async def test_failure_leaves_existing_file_tree_unchanged(self, db_session, monkeypatch):
        sprint = _seed_sprint(db_session)
        sprint.repo.file_tree = "old.py"

        async def _boom(*args, **kwargs):
            raise RuntimeError("GitHub unreachable")

        monkeypatch.setattr(readme_utils, "fetch_repo_metadata", _boom)

        result = await readme_utils.refresh_file_tree(sprint)

        assert result == "old.py"
        assert sprint.repo.file_tree == "old.py"

    @pytest.mark.asyncio
    async def test_no_repo_returns_none(self, db_session):
        sprint = _seed_sprint(db_session)
        sprint.repo = None

        result = await readme_utils.refresh_file_tree(sprint)

        assert result is None
