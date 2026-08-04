"""Tests for backend/config.py — env var helpers and module-level constants."""

import importlib
import os

import pytest

import backend.config


class TestGetBool:
    """Tests for ``_get_bool``."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            (" true ", True),
        ],
    )
    def test_truthy_values(self, monkeypatch, value, expected):
        monkeypatch.setenv("TEST_BOOL", value)
        assert backend.config._get_bool("TEST_BOOL") is expected

    @pytest.mark.parametrize(
        "value",
        ["false", "False", "FALSE", "0", "1", "yes", "no", "anything"],
    )
    def test_non_true_defaults_to_false(self, monkeypatch, value):
        monkeypatch.setenv("TEST_BOOL", value)
        assert backend.config._get_bool("TEST_BOOL") is False

    def test_unset_returns_default_true(self, monkeypatch):
        monkeypatch.delenv("TEST_BOOL", raising=False)
        assert backend.config._get_bool("TEST_BOOL", default=True) is True

    def test_unset_defaults_to_false(self, monkeypatch):
        monkeypatch.delenv("TEST_BOOL", raising=False)
        assert backend.config._get_bool("TEST_BOOL") is False


class TestGetInt:
    """Tests for ``_get_int``."""

    def test_valid_integer(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "42")
        assert backend.config._get_int("TEST_INT", 10) == 42

    def test_empty_returns_default(self, monkeypatch):
        monkeypatch.delenv("TEST_INT", raising=False)
        assert backend.config._get_int("TEST_INT", 99) == 99

    def test_invalid_string_returns_default(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "not-a-number")
        assert backend.config._get_int("TEST_INT", 7) == 7

    def test_zero(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "0")
        assert backend.config._get_int("TEST_INT", 10) == 0

    def test_negative(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "-5")
        assert backend.config._get_int("TEST_INT", 10) == -5


class TestGetList:
    """Tests for ``_get_list``."""

    def test_comma_separated_values(self, monkeypatch):
        monkeypatch.setenv("TEST_LIST", "a, b, c")
        assert backend.config._get_list("TEST_LIST", []) == ["a", "b", "c"]

    def test_empty_returns_default(self, monkeypatch):
        monkeypatch.delenv("TEST_LIST", raising=False)
        assert backend.config._get_list("TEST_LIST", ["fallback"]) == ["fallback"]

    def test_trims_whitespace(self, monkeypatch):
        monkeypatch.setenv("TEST_LIST", "  one , two  ,three ")
        assert backend.config._get_list("TEST_LIST", []) == ["one", "two", "three"]

    def test_single_value_no_commas(self, monkeypatch):
        monkeypatch.setenv("TEST_LIST", "only-one")
        assert backend.config._get_list("TEST_LIST", []) == ["only-one"]

    def test_blank_items_removed(self, monkeypatch):
        monkeypatch.setenv("TEST_LIST", "a,,b, ,c")
        assert backend.config._get_list("TEST_LIST", []) == ["a", "b", "c"]


class TestGetOptionalPath:
    """Tests for ``_get_optional_path``."""

    def test_set_returns_normpath(self, monkeypatch):
        monkeypatch.setenv("TEST_PATH", "foo/bar/../baz")
        result = backend.config._get_optional_path("TEST_PATH")
        assert result == os.path.normpath("foo/baz")

    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("TEST_PATH", raising=False)
        assert backend.config._get_optional_path("TEST_PATH") is None


class TestModuleConstants:
    """Tests for module-level constants loaded at import time."""

    def test_defaults_when_env_is_unset(self, monkeypatch):
        """Module constants should use their default values when no env vars are set."""
        monkeypatch.setattr("dotenv.load_dotenv", lambda: None)

        monkeypatch.delenv("STORE_OFFLINE", raising=False)
        monkeypatch.delenv("STORAGE_LOCATION", raising=False)
        monkeypatch.delenv("MAX_UPLOAD_SIZE_MB", raising=False)
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        monkeypatch.delenv("VERSION", raising=False)

        importlib.reload(backend.config)

        assert backend.config.STORE_OFFLINE is False
        assert os.path.normpath("./uploads") == backend.config.STORAGE_LOCATION
        assert backend.config.MAX_UPLOAD_SIZE_MB == 100
        assert backend.config.CORS_ORIGINS == ["http://localhost:5173"]
        assert backend.config.VERSION == "0.1.0"

    def test_store_offline_set_to_true(self, monkeypatch):
        monkeypatch.setenv("STORE_OFFLINE", "true")
        monkeypatch.delenv("STORAGE_LOCATION", raising=False)
        importlib.reload(backend.config)
        assert backend.config.STORE_OFFLINE is True

    def test_cors_origins_parsed(self, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", "http://a:3000,http://b:8080")
        monkeypatch.delenv("STORE_OFFLINE", raising=False)
        importlib.reload(backend.config)
        assert backend.config.CORS_ORIGINS == ["http://a:3000", "http://b:8080"]

    def test_max_upload_size_mb_parsed(self, monkeypatch):
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "50")
        monkeypatch.delenv("STORE_OFFLINE", raising=False)
        importlib.reload(backend.config)
        assert backend.config.MAX_UPLOAD_SIZE_MB == 50

    def test_version_custom(self, monkeypatch):
        monkeypatch.setenv("VERSION", "2.0.0")
        monkeypatch.delenv("STORE_OFFLINE", raising=False)
        importlib.reload(backend.config)
        assert backend.config.VERSION == "2.0.0"

    def test_database_url_default(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        importlib.reload(backend.config)
        assert "postgresql://" in backend.config.DATABASE_URL

    def test_encryption_key_default_empty(self, monkeypatch):
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        importlib.reload(backend.config)
        assert backend.config.ENCRYPTION_KEY == ""

    def test_github_api_timeout_default(self, monkeypatch):
        monkeypatch.delenv("GITHUB_API_TIMEOUT", raising=False)
        importlib.reload(backend.config)
        assert backend.config.GITHUB_API_TIMEOUT == 15

    def test_issue_tracker_timeout_default(self, monkeypatch):
        monkeypatch.delenv("ISSUE_TRACKER_TIMEOUT", raising=False)
        importlib.reload(backend.config)
        assert backend.config.ISSUE_TRACKER_TIMEOUT == 15

    def test_issue_tracker_timeout_override(self, monkeypatch):
        monkeypatch.setenv("ISSUE_TRACKER_TIMEOUT", "45")
        importlib.reload(backend.config)
        assert backend.config.ISSUE_TRACKER_TIMEOUT == 45

    def test_redis_config_defaults(self, monkeypatch):
        monkeypatch.delenv("REDIS_HOST", raising=False)
        monkeypatch.delenv("REDIS_PORT", raising=False)
        monkeypatch.delenv("REDIS_DB", raising=False)
        importlib.reload(backend.config)
        assert backend.config.REDIS_HOST == "localhost"
        assert backend.config.REDIS_PORT == 6379
        assert backend.config.REDIS_DB == 0
