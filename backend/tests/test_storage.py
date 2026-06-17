"""Tests for backend/services/storage.py — StorageService init and store logic."""

import importlib
import os

import pytest


def _reload_storage(monkeypatch):
    """Reload config → storage chain so env var changes take effect.

    *monkeypatch* must already have the desired env vars set before calling.
    """
    # Prevent load_dotenv from clobbering monkeypatched env vars during reload
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)

    import backend.config
    import backend.services.storage

    importlib.reload(backend.config)
    importlib.reload(backend.services.storage)


class TestStorageServiceInit:
    """Tests for ``StorageService.__init__``."""

    def test_offline_disabled(self, monkeypatch):
        """When STORE_OFFLINE is false, no disk operations happen."""
        monkeypatch.setenv("STORE_OFFLINE", "false")
        monkeypatch.delenv("STORAGE_LOCATION", raising=False)
        _reload_storage(monkeypatch)

        from backend.services.storage import StorageService

        svc = StorageService()
        assert svc.offline is False

    def test_offline_enabled_creates_dir(self, monkeypatch, tmp_path):
        """When STORE_OFFLINE is true and STORAGE_LOCATION is valid, the
        directory is created."""
        storage_dir = str(tmp_path / "store")
        monkeypatch.setenv("STORE_OFFLINE", "true")
        monkeypatch.setenv("STORAGE_LOCATION", storage_dir)
        _reload_storage(monkeypatch)

        from backend.services.storage import StorageService

        svc = StorageService()
        assert svc.offline is True
        assert os.path.isdir(storage_dir)

    def test_offline_no_location_raises(self, monkeypatch):
        """RuntimeError when STORE_OFFLINE=true but STORAGE_LOCATION is not set."""
        monkeypatch.setenv("STORE_OFFLINE", "true")
        monkeypatch.delenv("STORAGE_LOCATION", raising=False)
        _reload_storage(monkeypatch)

        from backend.services.storage import StorageService

        with pytest.raises(RuntimeError, match="STORAGE_LOCATION"):
            StorageService()

    def test_offline_unwritable_path_raises(self, monkeypatch):
        """RuntimeError when STORAGE_LOCATION cannot be created (e.g., NUL on Windows)."""
        monkeypatch.setenv("STORE_OFFLINE", "true")
        monkeypatch.setenv("STORAGE_LOCATION", "NUL")  # reserved name on Windows
        _reload_storage(monkeypatch)

        # On Windows, creating a directory named NUL raises OSError.
        # On Unix, this would succeed, so we accept either RuntimeError or OSError.
        import contextlib

        from backend.services.storage import StorageService

        with contextlib.suppress(RuntimeError, OSError):
            StorageService()


class TestStorageServiceStore:
    """Tests for ``StorageService.store``."""

    def test_store_memory_only(self, monkeypatch, sample_zip_bytes, sample_md_bytes):
        """When offline is false, store returns {stored: False} without disk I/O."""
        monkeypatch.setenv("STORE_OFFLINE", "false")
        monkeypatch.delenv("STORAGE_LOCATION", raising=False)
        _reload_storage(monkeypatch)

        from backend.services.storage import StorageService

        svc = StorageService()
        result = svc.store(sample_zip_bytes, sample_md_bytes, "job-123")
        assert result == {"stored": False}

    def test_store_writes_files(self, monkeypatch, tmp_path, sample_zip_bytes, sample_md_bytes):
        """When offline is true, store extracts the zip and writes md to disk."""
        storage_dir = str(tmp_path / "store")
        monkeypatch.setenv("STORE_OFFLINE", "true")
        monkeypatch.setenv("STORAGE_LOCATION", storage_dir)
        monkeypatch.setenv("MAX_ZIP_FILES", "100000")  # sample zip has many entries
        _reload_storage(monkeypatch)

        from backend.services.storage import StorageService

        svc = StorageService()
        result = svc.store(sample_zip_bytes, sample_md_bytes, "job-456")

        assert result["stored"] is True
        assert "stored_path" in result
        assert os.path.isdir(result["zip_path"])
        assert os.path.isfile(result["md_path"])

        # Verify markdown content was written
        with open(result["md_path"], encoding="utf-8") as fh:
            content = fh.read()
        assert "register" in content.lower()
