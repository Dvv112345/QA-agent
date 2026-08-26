"""Tests for the finding-environment description helpers.

Every helper here promises never to raise, so the degraded paths matter as
much as the happy one: a finding whose page has already closed must still
get a usable environment string.
"""

import platform
from importlib.metadata import PackageNotFoundError

from backend.utils import environment_utils
from backend.utils.environment_utils import (
    browser_environment,
    ci_variable_values,
    os_environment,
    script_environment,
    url_values,
    variable_and_secret_names,
)


class TestOsEnvironment:
    def test_reports_the_platform(self):
        assert os_environment() == platform.platform()

    def test_falls_back_when_platform_probing_fails(self, monkeypatch):
        monkeypatch.setattr(
            environment_utils.platform,
            "platform",
            lambda: (_ for _ in ()).throw(OSError("boom")),
        )
        assert os_environment()  # non-empty, and did not raise


class TestScriptEnvironment:
    def test_includes_os_and_python_version(self):
        result = script_environment()
        assert platform.platform() in result
        assert f"Python {platform.python_version()}" in result

    def test_omits_playwright_when_not_installed(self, monkeypatch):
        def _missing(name):
            raise PackageNotFoundError(name)

        monkeypatch.setattr(environment_utils, "version", _missing)
        result = script_environment()
        assert "Playwright" not in result
        assert f"Python {platform.python_version()}" in result

    def test_includes_playwright_version_when_available(self, monkeypatch):
        monkeypatch.setattr(environment_utils, "version", lambda name: "1.49.0")
        assert "Playwright 1.49.0" in script_environment()

    def test_survives_a_metadata_lookup_failing_unexpectedly(self, monkeypatch):
        """A damaged install can raise more than PackageNotFoundError, and
        this module promises never to raise."""

        def _broken(name):
            raise RuntimeError("metadata is corrupt")

        monkeypatch.setattr(environment_utils, "version", _broken)
        result = script_environment()
        assert platform.platform() in result
        assert "Playwright" not in result


class TestBrowserEnvironment:
    def test_includes_every_part(self):
        result = browser_environment(
            "Chromium 131.0.6778.85",
            {"width": 1280, "height": 720},
            "https://app.test/checkout",
        )
        assert "Chromium 131.0.6778.85" in result
        assert "viewport 1280x720" in result
        assert platform.platform() in result
        assert "https://app.test/checkout" in result

    def test_omits_viewport_when_unavailable(self):
        result = browser_environment("Chromium 131", None, "https://app.test/")
        assert "viewport" not in result
        assert "Chromium 131" in result

    def test_omits_url_when_unavailable(self):
        result = browser_environment("Chromium 131", {"width": 800, "height": 600}, None)
        assert result.endswith(platform.platform())

    def test_survives_a_viewport_that_is_not_a_mapping(self):
        """Callers use this unguarded, so an AttributeError here would end a
        session and discard the whole action log."""
        result = browser_environment("Chromium 131", "1280x720", "https://app.test/")

        assert "viewport" not in result
        assert "Chromium 131" in result
        assert "https://app.test/" in result

    def test_falls_back_to_os_alone(self):
        """A session constructed without a browser still describes something."""
        assert browser_environment(None, None, None) == platform.platform()


class TestCiVariableValues:
    """The narrower half of the URL question.

    `url_values` answers "may a human be shown this in a ticket";
    `ci_variable_values` answers "may this sit in a world-readable CI store
    and go unmasked in job logs". Reusing one classifier for both failed
    open — a credential-bearing URL was filed as a plain CI variable.
    """

    def test_a_plain_http_url_is_a_variable(self):
        env = {"BASE_URL": "https://staging.example.com/app"}

        assert ci_variable_values(env) == {"https://staging.example.com/app"}

    def test_basic_auth_credentials_fall_through_to_secret(self):
        env = {"BASE_URL": "https://qa:hunter2@staging.example.com"}

        assert ci_variable_values(env) == set()
        # Still readable in a ticket — the other question, unchanged.
        assert url_values(env) == {"https://qa:hunter2@staging.example.com"}

    def test_a_query_string_falls_through_to_secret(self):
        """Webhooks and signed URLs carry their token here."""
        env = {"HOOK": "https://hooks.example.com/services/AB?token=xyz"}

        assert ci_variable_values(env) == set()

    def test_a_non_url_is_never_a_variable(self):
        assert ci_variable_values({"BROWSER": "chromium", "PORT": "8080"}) == set()

    def test_no_env_vars_is_not_an_error(self):
        assert ci_variable_values(None) == set()
        assert ci_variable_values({}) == set()


class TestVariableAndSecretNames:
    def test_splits_names_by_what_they_become_in_ci(self):
        variables, secrets = variable_and_secret_names(
            {
                "BASE_URL": "https://staging.example.com",
                "DB_PASSWORD": "hunter2",
                "ADMIN_URL": "https://admin:pw@staging.example.com",
            }
        )

        assert variables == ["BASE_URL"]
        # The credential-bearing URL lands in the safer store.
        assert secrets == ["DB_PASSWORD", "ADMIN_URL"]

    def test_an_empty_environment_yields_two_empty_lists(self):
        assert variable_and_secret_names(None) == ([], [])
