"""Tests for backend/services/browser_session.py — Playwright fully mocked.

No real browser runs here, mirroring how ``test_execute_test.py`` mocks
``script_runner``: CI verifies the executor logic (origin lock, secret
handling, the never-raise contract) and manual e2e verifies that the
Playwright calls underneath actually drive a page.
"""

import json
import time
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

from backend.services import browser_session
from backend.services.browser_session import BrowserSession, FindingRecord, allowed_origins


class _FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    def _record(self, action, *args):
        if self._selector in self._page.broken_selectors:
            raise PlaywrightError("Timeout 10000ms exceeded.")
        self._page.actions.append((action, self._selector, *args))
        # A click or keypress can follow a link, which is how a page leaves
        # the allowed origins without navigate() ever being called.
        if self._page.action_navigates_to is not None:
            self._page.url = self._page.action_navigates_to

    def click(self):
        self._record("click")

    def fill(self, value):
        self._record("fill", value)

    def press(self, key):
        self._record("press", key)

    def aria_snapshot(self, mode=None):
        if self._page.snapshot_error:
            raise PlaywrightError("snapshot boom")
        return self._page.snapshot_text


class _FakePage:
    """Minimal stand-in for a Playwright Page."""

    def __init__(self, url="https://app.test/", title="App"):
        self._url = url
        self._title = title
        self.actions: list[tuple] = []
        self.broken_selectors: set[str] = set()
        self.snapshot_text = '- button "Export" [ref=e3]'
        self.snapshot_error = False
        self.screenshot_bytes = b"PNGDATA"
        self.screenshot_error = False
        self.screenshot_calls = 0
        self.goto_error: str | None = None
        self.redirect_to: str | None = None
        self.action_navigates_to: str | None = None
        self.handlers: dict = {}
        self.viewport = None
        self.viewport_size: dict | None = {"width": 1280, "height": 720}
        self.url_error = False
        self.default_timeout = None

    # A property so a test can make the page stop answering for its URL,
    # the way a crashed or closed page does.
    @property
    def url(self):
        if self.url_error:
            raise PlaywrightError("page has been closed")
        return self._url

    @url.setter
    def url(self, value):
        self._url = value

    def set_default_timeout(self, ms):
        self.default_timeout = ms

    def on(self, event, handler):
        self.handlers[event] = handler

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def title(self):
        return self._title

    def goto(self, url):
        if self.goto_error:
            raise PlaywrightError(self.goto_error)
        self.url = self.redirect_to or url

    def go_back(self):
        self.url = "https://app.test/"

    def go_forward(self):
        self.url = "https://app.test/forward"

    def set_viewport_size(self, size):
        self.viewport = size
        self.viewport_size = size

    def screenshot(self):
        self.screenshot_calls += 1
        if self.screenshot_error:
            raise PlaywrightError("screenshot boom")
        return self.screenshot_bytes


def _session(page, **kwargs):
    """Build a BrowserSession wired to *page*, bypassing __enter__."""
    kwargs.setdefault("base_urls", ["https://app.test/login"])
    kwargs.setdefault("env_vars", {"APP_URL": "https://app.test", "PW": "hunter2"})
    kwargs.setdefault("on_finding", lambda record, png: None)
    session = BrowserSession(**kwargs)
    session._page = page
    return session


class TestAllowedOrigins:
    def test_reduces_urls_to_scheme_and_netloc(self):
        assert allowed_origins(["https://app.test/login?x=1"]) == {("https", "app.test")}

    def test_multiple_origins(self):
        origins = allowed_origins(["https://app.test", "https://api.test:8443/v1"])
        assert origins == {("https", "app.test"), ("https", "api.test:8443")}

    def test_ignores_non_http_urls(self):
        assert allowed_origins(["postgresql://user:pw@db:5432/app"]) == set()


class TestNavigate:
    def test_allows_url_on_nominated_origin(self):
        page = _FakePage()
        result = _session(page).navigate("https://app.test/reports")
        assert "Navigated to https://app.test/reports" in result

    def test_allows_a_second_nominated_origin(self):
        page = _FakePage()
        session = _session(page, base_urls=["https://app.test", "https://api.test"])
        assert "Navigated" in session.navigate("https://api.test/v1/health")

    def test_refuses_off_origin(self):
        page = _FakePage()
        result = _session(page).navigate("https://evil.test/steal")
        assert result.startswith("ERROR:")
        assert "not allowed" in result
        assert "https://app.test" in result  # tells the model where it may go

    def test_refused_navigation_does_not_touch_the_page(self):
        page = _FakePage()
        _session(page).navigate("https://evil.test/steal")
        assert page.url == "https://app.test/"

    def test_catches_off_origin_redirect_after_settle(self):
        page = _FakePage()
        page.redirect_to = "https://evil.test/landed"
        result = _session(page).navigate("https://app.test/redirector")
        assert result.startswith("ERROR:")
        assert "redirected to" in result
        assert "Navigation was undone" in result

    def test_goto_failure_returns_error_string(self):
        page = _FakePage()
        page.goto_error = "net::ERR_CONNECTION_REFUSED"
        result = _session(page).navigate("https://app.test/down")
        assert result.startswith("ERROR:")
        assert "ERR_CONNECTION_REFUSED" in result


class TestOffOriginDrift:
    """Link-driven navigation off the app is allowed, but never silent.

    Following the application's own links (an OAuth hop, an external
    reference the charter asks about) can be exactly what a charter calls
    for, so it is not blocked. What must not happen is the model probing a
    third-party page without realising it left.
    """

    def test_off_origin_click_still_succeeds(self):
        page = _FakePage()
        page.action_navigates_to = "https://partner.test/oauth"
        result = _session(page).click("e3")
        assert not result.startswith("ERROR:")
        assert page.url == "https://partner.test/oauth"  # not undone

    def test_off_origin_click_carries_the_notice(self):
        page = _FakePage()
        page.action_navigates_to = "https://partner.test/oauth"
        result = _session(page).click("e3")
        assert "outside the application under test" in result
        assert "https://app.test" in result  # names where it may return to

    def test_on_origin_click_has_no_notice(self):
        page = _FakePage()
        page.action_navigates_to = "https://app.test/reports"
        result = _session(page).click("e3")
        assert "outside the application" not in result

    def test_press_and_history_carry_the_notice(self):
        page = _FakePage()
        page.action_navigates_to = "https://partner.test/oauth"
        assert "outside the application" in _session(page).press("e3", "Enter")

        page = _FakePage(url="https://partner.test/oauth")
        session = _session(page)
        page.go_forward = lambda: setattr(page, "url", "https://partner.test/next")
        assert "outside the application" in session.go_forward()

    def test_snapshot_header_carries_the_notice_while_off_origin(self):
        """The load-bearing one: the reminder persists, not just at the click."""
        page = _FakePage(url="https://partner.test/oauth")
        result = _session(page).snapshot()
        assert "outside the application under test" in result

    def test_snapshot_header_is_clean_while_on_origin(self):
        page = _FakePage(url="https://app.test/reports")
        assert "outside the application" not in _session(page).snapshot()

    def test_navigating_back_clears_the_notice(self):
        page = _FakePage(url="https://partner.test/oauth")
        session = _session(page)
        assert "outside the application" in session.snapshot()
        session.navigate("https://app.test/reports")
        assert "outside the application" not in session.snapshot()


class TestSnapshot:
    def test_includes_url_and_title(self):
        page = _FakePage(url="https://app.test/reports", title="Reports")
        result = _session(page).snapshot()
        assert "https://app.test/reports" in result
        assert "Reports" in result
        assert "ref=e3" in result

    def test_truncates_past_the_cap(self, monkeypatch):
        monkeypatch.setattr(browser_session, "EXPLORATORY_SNAPSHOT_MAX_CHARS", 50)
        page = _FakePage()
        page.snapshot_text = "x" * 500
        result = _session(page).snapshot()
        assert "snapshot truncated" in result
        assert len(result) < 300

    def test_failure_returns_error_string(self):
        page = _FakePage()
        page.snapshot_error = True
        result = _session(page).snapshot()
        assert result.startswith("ERROR:")


class TestElementActions:
    def test_click_dispatches_aria_ref_selector(self):
        page = _FakePage()
        _session(page).click("e3")
        assert page.actions == [("click", "aria-ref=e3")]

    def test_fill_passes_value(self):
        page = _FakePage()
        _session(page).fill("e7", "hello@example.com")
        assert page.actions == [("fill", "aria-ref=e7", "hello@example.com")]

    def test_press_passes_key(self):
        page = _FakePage()
        _session(page).press("e7", "Enter")
        assert page.actions == [("press", "aria-ref=e7", "Enter")]

    def test_stale_ref_pushes_toward_resnapshotting(self):
        page = _FakePage()
        page.broken_selectors.add("aria-ref=e99")
        result = _session(page).click("e99")
        assert result.startswith("ERROR:")
        assert "call snapshot" in result

    def test_executor_never_raises(self):
        """The never-raise contract — a killed session cannot recover."""
        page = _FakePage()
        page.broken_selectors.add("aria-ref=e1")
        for call in (
            lambda s: s.click("e1"),
            lambda s: s.fill("e1", "x"),
            lambda s: s.press("e1", "Enter"),
            lambda s: s.fill_secret("e1", "PW"),
        ):
            assert call(_session(page)).startswith("ERROR:")


class TestFillSecret:
    def test_resolves_value_from_env_vars(self):
        page = _FakePage()
        _session(page).fill_secret("e8", "PW")
        assert page.actions == [("fill", "aria-ref=e8", "hunter2")]

    def test_result_never_contains_the_literal(self):
        page = _FakePage()
        result = _session(page).fill_secret("e8", "PW")
        assert "hunter2" not in result
        assert "PW" in result

    def test_unknown_variable_lists_available_names(self):
        page = _FakePage()
        result = _session(page).fill_secret("e8", "NOPE")
        assert result.startswith("ERROR:")
        assert "APP_URL" in result
        assert "hunter2" not in result  # names only, never values


class TestReadConsole:
    def test_empty_when_nothing_recorded(self):
        assert "No console errors" in _session(_FakePage()).read_console()

    def test_drains_recorded_messages(self):
        session = _session(_FakePage())
        session._console.extend(["console.error: boom", "request failed: /api/x (500)"])
        result = session.read_console()
        assert "boom" in result
        assert "/api/x" in result
        assert "No console errors" in session.read_console()  # drained

    def test_console_handler_filters_to_errors_and_warnings(self):
        session = _session(_FakePage())
        session._record_console(SimpleNamespace(type="log", text="chatty"))
        session._record_console(SimpleNamespace(type="error", text="boom"))
        assert session._console == ["console.error: boom"]


class TestRecordFinding:
    def test_invokes_callback_with_record_and_screenshot(self):
        page = _FakePage()
        seen: list = []
        session = _session(page, on_finding=lambda record, png: seen.append((record, png)))

        result = session.record_finding(
            finding_type="bug",
            severity="high",
            title="Empty export",
            steps_to_reproduce="Open reports\nClick Export",
            expected="A CSV with a header",
            actual="Zero bytes",
        )

        assert len(seen) == 1
        record, png = seen[0]
        assert isinstance(record, FindingRecord)
        assert record.title == "Empty export"
        assert png == b"PNGDATA"
        assert page.screenshot_calls == 1  # capturing is the default
        assert "Recorded bug" in result

    def test_records_where_the_observation_was_made(self):
        page = _FakePage(url="https://app.test/checkout")
        seen: list = []
        session = _session(page, on_finding=lambda record, png: seen.append((record, png)))
        session._browser_label = "Chromium 131.0.6778.85"

        session.record_finding(title="Total omits tax")

        record, _ = seen[0]
        assert "Chromium 131.0.6778.85" in record.environment
        assert "viewport 1280x720" in record.environment
        assert "https://app.test/checkout" in record.environment

    def test_environment_reflects_the_viewport_at_that_moment(self):
        """A finding at 375px wide is a different finding — the viewport must
        describe this observation, not wherever the session ends up."""
        page = _FakePage()
        seen: list = []
        session = _session(page, on_finding=lambda record, png: seen.append((record, png)))

        session.set_viewport(375, 812)
        session.record_finding(title="Nav bar overlaps content")
        session.set_viewport(1280, 720)

        record, _ = seen[0]
        assert "viewport 375x812" in record.environment

    def test_environment_survives_a_page_that_cannot_answer(self):
        """A crashed page costs the detail, never the finding."""
        page = _FakePage()
        page.url_error = True
        page.viewport_size = None
        seen: list = []
        session = _session(page, on_finding=lambda record, png: seen.append((record, png)))

        result = session.record_finding(title="Something broke", page_still_shows_problem=False)

        record, _ = seen[0]
        assert record.environment  # non-empty: the OS is always available
        assert "viewport" not in record.environment
        assert "Recorded" in result

    def test_environment_without_a_browser_label(self):
        """Constructed directly, __enter__ never ran, so there is no label."""
        page = _FakePage()
        seen: list = []
        session = _session(page, on_finding=lambda record, png: seen.append((record, png)))

        session.record_finding(title="No browser handle")

        record, _ = seen[0]
        assert record.environment
        assert "Chromium" not in record.environment

    def test_skips_the_capture_when_the_page_has_moved_on(self):
        """False evidence is worse than none — an image of an unrelated page
        still reads as showing the defect."""
        page = _FakePage()
        seen: list = []
        session = _session(page, on_finding=lambda record, png: seen.append((record, png)))

        result = session.record_finding(
            title="Export lost the last row",
            page_still_shows_problem=False,
        )

        record, png = seen[0]
        assert record.title == "Export lost the last row"  # still filed
        assert png is None
        # Asserting only on the None cannot tell "skipped" from "captured and
        # thrown away", and only the former avoids the wasted call.
        assert page.screenshot_calls == 0
        assert "Recorded" in result

    def test_skipped_capture_still_counts_toward_the_limit(self):
        page = _FakePage()
        session = _session(page, max_findings=1)

        session.record_finding(title="First", page_still_shows_problem=False)
        result = session.record_finding(title="Second", page_still_shows_problem=False)

        assert "limit of 1 findings" in result

    def test_screenshot_failure_still_records_the_finding(self):
        page = _FakePage()
        page.screenshot_error = True
        seen: list = []
        session = _session(page, on_finding=lambda record, png: seen.append((record, png)))

        result = session.record_finding(title="Something odd")

        assert len(seen) == 1
        assert seen[0][1] is None
        assert not result.startswith("ERROR:")

    def test_persistence_failure_returns_error_without_raising(self):
        def boom(record, png):
            raise RuntimeError("db down")

        session = _session(_FakePage(), on_finding=boom)
        result = session.record_finding(title="x")
        assert result.startswith("ERROR:")
        assert session.findings_recorded == 0

    def test_finding_limit_enforced(self):
        session = _session(_FakePage(), max_findings=2)
        for _ in range(2):
            assert "Recorded" in session.record_finding(title="x")
        result = session.record_finding(title="one too many")
        assert result.startswith("ERROR:")
        assert "limit of 2" in result
        assert session.findings_recorded == 2


class TestToolRegistry:
    def test_covers_every_browser_tool(self):
        """Registry and prompt schema must not drift apart."""
        from backend.services.llm_prompts import BROWSER_TOOLS

        registry = _session(_FakePage()).tool_registry()
        schema_names = {tool["function"]["name"] for tool in BROWSER_TOOLS}

        # finish_session is terminal and handled by the loop, never dispatched.
        assert set(registry) == schema_names - {"finish_session"}

    def test_registry_entries_are_callable(self):
        for executor in _session(_FakePage()).tool_registry().values():
            assert callable(executor)


def _patch_playwright(monkeypatch, page):
    """Wire sync_playwright to hand back *page*; returns the close-order log."""
    closed: list[str] = []
    browser = SimpleNamespace(new_page=lambda: page, close=lambda: closed.append("browser"))
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=lambda headless: browser),
        stop=lambda: closed.append("playwright"),
    )
    monkeypatch.setattr(
        browser_session, "sync_playwright", lambda: SimpleNamespace(start=lambda: playwright)
    )
    return closed


class TestOpensOnTheApplication:
    """__enter__ navigates to base_urls[0] instead of leaving about:blank.

    Without it the model's first action is spent discovering where the
    application lives, since it only ever sees env var *names*.
    """

    def test_enter_opens_the_first_base_url(self, monkeypatch):
        page = _FakePage(url="about:blank")
        _patch_playwright(monkeypatch, page)

        with BrowserSession(
            base_urls=["https://app.test/home", "https://api.test"],
            env_vars={},
            on_finding=lambda r, p: None,
        ):
            assert page.url == "https://app.test/home"

    def test_empty_base_urls_does_not_raise(self, monkeypatch):
        """BrowserSession is constructible directly — an IndexError here would
        escape the never-raise contract and kill the session before any
        executor ran."""
        page = _FakePage(url="about:blank")
        _patch_playwright(monkeypatch, page)

        with BrowserSession(base_urls=[], env_vars={}, on_finding=lambda r, p: None) as session:
            assert session._page is page
            assert page.url == "about:blank"

    def test_unreachable_application_still_yields_a_usable_session(self, monkeypatch):
        """A dead app is itself a finding — it must not kill the session."""
        page = _FakePage(url="about:blank")
        page.goto_error = "net::ERR_CONNECTION_REFUSED"
        _patch_playwright(monkeypatch, page)

        with BrowserSession(
            base_urls=["https://app.test"], env_vars={}, on_finding=lambda r, p: None
        ) as session:
            assert session.snapshot().startswith("Current URL:")

    # ── the opening navigation fires the arrival hook ──────────────────
    #
    # `on_navigated` is bound in the constructor, so the navigation
    # `__enter__` performs is a real arrival like any other. A caller that
    # assumed otherwise — attaching its catalogue only after `__enter__`
    # returned — could not examine the landing page at all, and every test
    # here used to bypass `__enter__`, so nothing said so.

    def _entered(self, monkeypatch, page, base_urls=("https://app.test/home",)):
        arrivals: list[str] = []
        _patch_playwright(monkeypatch, page)
        with BrowserSession(
            base_urls=list(base_urls),
            env_vars={},
            on_finding=lambda r, p: None,
            on_navigated=lambda url, deadline: arrivals.append(url) or "",
        ):
            pass
        return arrivals

    def test_enter_fires_the_arrival_hook_for_the_first_base_url(self, monkeypatch):
        arrivals = self._entered(monkeypatch, _FakePage(url="about:blank"))

        assert arrivals == ["https://app.test/home"]

    def test_enter_fires_the_hook_with_where_it_landed_not_what_was_asked(self, monkeypatch):
        """A redirect makes these two different, and only one is the page.

        Filing the landing page's evidence under the pre-redirect URL is
        wrong evidence on a bug report.
        """
        page = _FakePage(url="about:blank")
        page.redirect_to = "https://app.test/login"

        assert self._entered(monkeypatch, page) == ["https://app.test/login"]

    def test_a_failed_opening_navigation_fires_nothing(self, monkeypatch):
        """Nothing arrived, so there is no page to examine.

        The caller detects this by the hook never having fired, and records
        the URL as unreachable rather than examining `about:blank` and
        calling the application clean.
        """
        page = _FakePage(url="about:blank")
        page.goto_error = "net::ERR_CONNECTION_REFUSED"

        assert self._entered(monkeypatch, page) == []

    def test_only_the_first_base_url_is_opened(self, monkeypatch):
        """Later base URLs are the caller's job to navigate to."""
        arrivals = self._entered(
            monkeypatch,
            _FakePage(url="about:blank"),
            base_urls=("https://app.test/home", "https://api.test/v1"),
        )

        assert arrivals == ["https://app.test/home"]


class TestLifecycle:
    def test_enter_launches_and_configures(self, monkeypatch):
        page = _FakePage()
        browser = SimpleNamespace(new_page=lambda: page, close=lambda: closed.append("browser"))
        closed: list[str] = []
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch=lambda headless: browser),
            stop=lambda: closed.append("playwright"),
        )
        monkeypatch.setattr(
            browser_session, "sync_playwright", lambda: SimpleNamespace(start=lambda: playwright)
        )

        with BrowserSession(
            base_urls=["https://app.test"], env_vars={}, on_finding=lambda r, p: None
        ) as session:
            assert session._page is page
            assert page.default_timeout == browser_session.EXPLORATORY_ACTION_TIMEOUT * 1000

        assert closed == ["browser", "playwright"]

    def test_browser_closed_even_when_body_raises(self, monkeypatch):
        closed: list[str] = []
        page = _FakePage()
        browser = SimpleNamespace(new_page=lambda: page, close=lambda: closed.append("browser"))
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch=lambda headless: browser),
            stop=lambda: closed.append("playwright"),
        )
        monkeypatch.setattr(
            browser_session, "sync_playwright", lambda: SimpleNamespace(start=lambda: playwright)
        )

        with (
            pytest.raises(RuntimeError),
            BrowserSession(
                base_urls=["https://app.test"], env_vars={}, on_finding=lambda r, p: None
            ),
        ):
            raise RuntimeError("session body blew up")

        assert closed == ["browser", "playwright"]


# ── Nonfunctional extensions ══════════════════════════════════════════


class _FakeRequest:
    def __init__(self, url, resource_type="xhr"):
        self.url = url
        self.resource_type = resource_type

    def is_navigation_request(self):
        return self.resource_type == "document"


class _FakeResponse:
    def __init__(self, url, status=200, headers=None, resource_type="document"):
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.request = _FakeRequest(url, resource_type)


class TestOnNavigatedHook:
    def _session_with_hook(self, page, **kwargs):
        calls: list[tuple[str, float]] = []

        def hook(url, deadline):
            calls.append((url, deadline))
            return "Examined."

        session = _session(page, on_navigated=hook, **kwargs)
        return session, calls

    def test_fires_once_on_a_real_url_change(self):
        page = _FakePage()
        session, calls = self._session_with_hook(page)

        result = session.navigate("https://app.test/reports")

        assert [url for url, _ in calls] == ["https://app.test/reports"]
        assert "Examined." in result

    def test_does_not_fire_when_the_url_did_not_change(self):
        page = _FakePage(url="https://app.test/reports")
        session, calls = self._session_with_hook(page)

        session.click("e3")  # no action_navigates_to — same page

        assert calls == []

    def test_fires_for_a_link_driven_navigation(self):
        page = _FakePage()
        page.action_navigates_to = "https://app.test/next"
        session, calls = self._session_with_hook(page)

        session.click("e3")

        assert [url for url, _ in calls] == ["https://app.test/next"]

    def test_does_not_fire_for_an_off_origin_redirect_navigate_undoes(self):
        """Ordering is load-bearing: the catalogue must not run on a third party."""
        page = _FakePage()
        page.redirect_to = "https://evil.test/landed"
        session, calls = self._session_with_hook(page)

        result = session.navigate("https://app.test/redirector")

        assert result.startswith("ERROR:")
        assert calls == []

    def test_a_callback_that_raises_does_not_break_the_executor(self):
        page = _FakePage()
        page.action_navigates_to = "https://app.test/next"

        def boom(url, deadline):
            raise OSError("No space left on device")

        session = _session(page, on_navigated=boom)

        result = session.click("e3")

        assert isinstance(result, str)
        assert not result.startswith("ERROR:")

    def test_the_callback_is_given_a_deadline_from_the_catalogue_budget(self):
        page = _FakePage()
        session, calls = self._session_with_hook(page, catalogue_timeout=30)

        session.navigate("https://app.test/reports")

        _url, deadline = calls[0]
        assert 0 < deadline - time.monotonic() <= 30

    def test_an_overrunning_callback_is_logged_and_still_returns(self, caplog):
        page = _FakePage()

        def slow(url, deadline):
            time.sleep(0.02)
            return "Examined."

        session = _session(page, on_navigated=slow, catalogue_timeout=0)

        with caplog.at_level("WARNING"):
            result = session.navigate("https://app.test/reports")

        assert "over its 0s budget" in caplog.text
        assert "Examined." in result

    def test_a_session_without_a_hook_behaves_exactly_as_before(self):
        page = _FakePage()
        assert _session(page).navigate("https://app.test/x").endswith("to see the page.")


class TestEndpointDiscovery:
    def test_a_successful_xhr_is_captured(self):
        session = _session(_FakePage())

        session._record_request(_FakeRequest("https://app.test/api/items"))

        assert session.discovered_endpoints == ["https://app.test/api/items"]

    def test_a_failed_request_reaches_the_console_and_not_the_endpoint_set(self):
        session = _session(_FakePage())

        session._record_request_failure(
            SimpleNamespace(url="https://app.test/api/items", failure="net::ERR")
        )

        assert session.discovered_endpoints == []
        assert "request failed" in session.read_console()

    def test_draining_the_console_does_not_empty_the_endpoint_set(self):
        session = _session(_FakePage())
        session._record_request(_FakeRequest("https://app.test/api/items"))
        session._record_console(SimpleNamespace(type="error", text="boom"))

        session.read_console()

        assert session.discovered_endpoints == ["https://app.test/api/items"]

    def test_documents_images_and_scripts_are_not_endpoints(self):
        session = _session(_FakePage())

        for kind in ("document", "image", "script", "stylesheet"):
            session._record_request(_FakeRequest(f"https://app.test/{kind}", resource_type=kind))

        assert session.discovered_endpoints == []

    def test_off_origin_calls_are_skipped(self):
        session = _session(_FakePage())

        session._record_request(_FakeRequest("https://analytics.example.com/collect"))

        assert session.discovered_endpoints == []

    def test_endpoints_dedupe_and_stay_bounded(self):
        session = _session(_FakePage())

        for _ in range(3):
            session._record_request(_FakeRequest("https://app.test/api/items#frag"))
        for n in range(browser_session.MAX_DISCOVERED_ENDPOINTS + 10):
            session._record_request(_FakeRequest(f"https://app.test/api/{n}"))

        assert session.discovered_endpoints[0] == "https://app.test/api/items"
        assert len(session.discovered_endpoints) == browser_session.MAX_DISCOVERED_ENDPOINTS


class TestCheckHeaders:
    def test_reads_the_captured_document_response(self):
        page = _FakePage(url="https://app.test/login")
        page.context = SimpleNamespace(
            cookies=lambda: [
                {"name": "session", "value": "s3cr3t", "secure": True, "httpOnly": True}
            ]
        )
        session = _session(page)
        session._record_response(_FakeResponse("https://app.test/login", 200, {"Server": "nginx"}))

        outcome = session.check_headers()

        assert outcome.ok
        assert outcome.data["status"] == 200
        assert outcome.data["headers"] == {"Server": "nginx"}
        assert outcome.data["cookies"] == [
            {"name": "session", "secure": True, "httpOnly": True, "sameSite": None}
        ]
        # Names and flags only — the value never leaves the browser.
        assert "s3cr3t" not in json.dumps(outcome.data)

    def test_no_captured_response_is_an_error_rather_than_a_clean_read(self):
        page = _FakePage(url="https://app.test/spa-route")
        session = _session(page)

        outcome = session.check_headers()

        assert not outcome.ok
        assert "no document response" in outcome.error

    def test_a_body_sample_is_read_only_for_an_error_response(self):
        page = _FakePage(url="https://app.test/boom")
        page.context = SimpleNamespace(cookies=lambda: [])
        page.content = lambda: "Traceback (most recent call last):"
        session = _session(page)

        session._record_response(_FakeResponse("https://app.test/boom", 500))
        assert "Traceback" in session.check_headers().data["body_sample"]

        session._record_response(_FakeResponse("https://app.test/boom", 200))
        assert session.check_headers().data["body_sample"] == ""


class TestDocumentContentType:
    """What the server served, which is the only thing that can say
    whether axe has a page to look at.  Chromium renders JSON through its
    own viewer, so the DOM always looks like HTML."""

    def test_reads_the_media_type_off_the_captured_response(self):
        page = _FakePage(url="https://app.test/api/reports")
        session = _session(page)
        session._record_response(
            _FakeResponse("https://app.test/api/reports", 404, {"content-type": "application/json"})
        )

        assert session.document_content_type() == "application/json"

    def test_parameters_and_case_are_stripped(self):
        page = _FakePage(url="https://app.test/login")
        session = _session(page)
        session._record_response(
            _FakeResponse(
                "https://app.test/login", 200, {"content-type": "TEXT/HTML; charset=UTF-8"}
            )
        )

        assert session.document_content_type() == "text/html"

    def test_no_captured_response_answers_unknown(self):
        # An in-page SPA navigation. The caller must read None as unknown,
        # never as "not a page".
        assert _session(_FakePage(url="https://app.test/spa-route")).document_content_type() is None

    def test_a_response_without_the_header_answers_unknown(self):
        page = _FakePage(url="https://app.test/login")
        session = _session(page)
        session._record_response(_FakeResponse("https://app.test/login", 200, {"Server": "nginx"}))

        assert session.document_content_type() is None


class TestAccessibilityAndPerformance:
    def test_a_refused_axe_run_returns_an_outcome_rather_than_raising(self, monkeypatch):
        import sys

        class _Axe:
            def run(self, page):
                raise RuntimeError("Execution context was destroyed")

        monkeypatch.setitem(
            sys.modules, "axe_playwright_python.sync_playwright", SimpleNamespace(Axe=_Axe)
        )

        outcome = _session(_FakePage()).scan_accessibility()

        assert not outcome.ok
        assert "axe could not run" in outcome.error

    def test_performance_timings_come_back_as_data(self):
        page = _FakePage()
        page.evaluate = lambda script: {"ttfb_ms": 12, "load_ms": 340}
        session = _session(page)

        outcome = session.measure_performance()

        assert outcome.ok
        assert outcome.data["load_ms"] == 340

    def test_an_unreadable_page_reports_failed_rather_than_zero(self):
        page = _FakePage()

        def boom(script):
            raise PlaywrightError("Execution context was destroyed")

        page.evaluate = boom

        outcome = _session(page).measure_performance()

        assert not outcome.ok

    def test_a_strange_evaluate_result_is_an_error(self):
        page = _FakePage()
        page.evaluate = lambda script: "undefined"

        assert not _session(page).measure_performance().ok


class TestCookiesForLoad:
    def test_returns_name_to_value(self):
        page = _FakePage()
        page.context = SimpleNamespace(
            cookies=lambda: [{"name": "session", "value": "abc"}, {"name": "", "value": "x"}]
        )

        assert _session(page).cookies_for_load() == {"session": "abc"}

    def test_a_browser_that_cannot_answer_gives_no_cookies(self):
        page = _FakePage()

        assert _session(page).cookies_for_load() == {}

    def test_cookie_values_never_appear_in_an_executor_return_string(self):
        page = _FakePage(url="https://app.test/x")
        page.context = SimpleNamespace(cookies=lambda: [{"name": "s", "value": "s3cr3t"}])
        session = _session(page)

        returned = " ".join(
            [
                session.snapshot(),
                session.navigate("https://app.test/y"),
                session.click("e3"),
                session.read_console(),
            ]
        )

        assert "s3cr3t" not in returned


class TestNonfunctionalRegistry:
    def test_it_omits_record_finding(self):
        registry = _session(_FakePage()).nonfunctional_tool_registry()

        assert "record_finding" not in registry
        assert "navigate" in registry
        assert "snapshot" in registry

    def test_the_exploratory_registry_is_untouched(self):
        assert "record_finding" in _session(_FakePage()).tool_registry()
