"""Tests for backend/config.py — the env var parsing helpers.

The module-level constants are deliberately untested: asserting a default
restates ``config.py`` rather than checking it, and doing so needs an
``importlib.reload`` that mutates global state other tests read.
"""

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
