"""Tests for backend/services/storage.py — conditional on-disk persistence."""

import os

import pytest

from backend.services import storage
from backend.services.storage import StorageService


@pytest.fixture
def offline_service(monkeypatch, tmp_path):
    """A StorageService writing into a temp dir with offline mode ON."""
    monkeypatch.setattr(storage, "STORE_OFFLINE", True)
    monkeypatch.setattr(storage, "STORAGE_LOCATION", str(tmp_path))
    return StorageService()


@pytest.fixture
def online_service(monkeypatch, tmp_path):
    """A StorageService with offline mode OFF — every write is a no-op."""
    monkeypatch.setattr(storage, "STORE_OFFLINE", False)
    monkeypatch.setattr(storage, "STORAGE_LOCATION", str(tmp_path))
    return StorageService()


class TestStoreReadme:
    def test_writes_when_offline(self, offline_service, tmp_path):
        path = offline_service.store_readme(b"# Hello", "sprint-1")
        assert path is not None
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as fh:
            assert fh.read() == "# Hello"

    def test_no_op_when_online(self, online_service):
        assert online_service.store_readme(b"# Hello", "sprint-1") is None

    def test_invalid_utf8_raises(self, offline_service):
        with pytest.raises(ValueError, match="not valid UTF-8"):
            offline_service.store_readme(b"\xff\xfe\x00", "sprint-1")


class TestStorePrd:
    def test_writes_bytes_verbatim_with_extension(self, offline_service):
        path = offline_service.store_prd(b"%PDF-1.4 binary", "sprint-1", "spec.PDF")
        assert path.endswith("PRD.pdf")  # extension lower-cased
        with open(path, "rb") as fh:
            assert fh.read() == b"%PDF-1.4 binary"

    def test_reupload_overwrites(self, offline_service):
        offline_service.store_prd(b"first", "sprint-1", "a.md")
        path = offline_service.store_prd(b"second", "sprint-1", "a.md")
        with open(path, "rb") as fh:
            assert fh.read() == b"second"

    def test_no_op_when_online(self, online_service):
        assert online_service.store_prd(b"x", "sprint-1", "a.md") is None


class TestStoreScreenshot:
    def test_writes_under_session_directory(self, offline_service, tmp_path):
        path = offline_service.store_screenshot(b"PNGDATA", "sprint-1", session_id=12, position=3)

        assert path is not None
        assert os.path.isfile(path)
        assert path.replace("\\", "/").endswith("sprint-1/exploratory/session_12/finding_3.png")
        with open(path, "rb") as fh:
            assert fh.read() == b"PNGDATA"

    def test_multiple_findings_in_one_session(self, offline_service):
        first = offline_service.store_screenshot(b"one", "sprint-1", 5, 0)
        second = offline_service.store_screenshot(b"two", "sprint-1", 5, 1)

        assert first != second
        assert os.path.isfile(first) and os.path.isfile(second)

    def test_sessions_are_isolated(self, offline_service):
        first = offline_service.store_screenshot(b"one", "sprint-1", 5, 0)
        second = offline_service.store_screenshot(b"two", "sprint-1", 6, 0)
        assert os.path.dirname(first) != os.path.dirname(second)

    def test_returns_none_when_offline_disabled(self, online_service):
        """STORE_OFFLINE=false means findings simply carry no screenshot.

        That is the documented outcome of the setting, not a failure — the
        caller must persist the finding regardless.
        """
        assert online_service.store_screenshot(b"PNGDATA", "sprint-1", 12, 0) is None

    def test_no_directory_created_when_offline_disabled(self, online_service, tmp_path):
        online_service.store_screenshot(b"PNGDATA", "sprint-1", 12, 0)
        assert not (tmp_path / "sprint-1").exists()


class TestConstruction:
    def test_raises_when_offline_without_location(self, monkeypatch):
        monkeypatch.setattr(storage, "STORE_OFFLINE", True)
        monkeypatch.setattr(storage, "STORAGE_LOCATION", "")
        with pytest.raises(RuntimeError, match="STORAGE_LOCATION"):
            StorageService()

    def test_creates_base_directory(self, monkeypatch, tmp_path):
        base = tmp_path / "nested" / "uploads"
        monkeypatch.setattr(storage, "STORE_OFFLINE", True)
        monkeypatch.setattr(storage, "STORAGE_LOCATION", str(base))
        StorageService()
        assert base.is_dir()
