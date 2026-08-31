"""Playwright browser session driven by the exploratory LLM tool loop.

Isolated from the worker task the same way ``script_runner`` is, and for the
same reason: the task's state machine stays testable without a real browser
in CI, and this module stays free of database imports.  Findings are handed
back through an injected ``on_finding`` callback rather than persisted here.

Every executor returns a string and **never raises** — the same contract as
``services/llm.py``'s ``read_file``, applied to a much larger tool surface.
A Playwright timeout, a detached element, a stale ref: all come back as text
the model can react to.  An executor that raised would kill a session that
could have recovered.

Runs inside the backend's own venv, so Playwright and its browser binaries
must be installed there (``playwright install chromium``).

Sync API by design: the caller must not have an asyncio loop running in this
thread.  ``tasks/explore_requirement.py`` resolves README/file-tree context
*before* opening a session for exactly this reason.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from backend.config import (
    EXPLORATORY_ACTION_TIMEOUT,
    EXPLORATORY_HEADLESS,
    EXPLORATORY_MAX_FINDINGS,
    EXPLORATORY_SNAPSHOT_MAX_CHARS,
    NONFUNCTIONAL_CATALOGUE_TIMEOUT,
)
from backend.utils import environment_utils

logger = logging.getLogger(__name__)

_SNAPSHOT_TRUNCATION_MARKER = "\n… (snapshot truncated)"

# Returned when the model addresses an element that no longer exists. Pushes
# hard toward re-snapshotting: a stale ref costs a full action timeout before
# it fails, which is a meaningful slice of a serial run's wall clock.
_STALE_REF = (
    "ERROR: no element matches ref {ref!r} on the current page. The page has "
    "changed since your last snapshot — call snapshot to get fresh refs "
    "before interacting again."
)

# Endpoint URLs one session remembers. Bounded because a chatty SPA can
# poll the same shape of URL indefinitely, and the run examines at most
# NONFUNCTIONAL_MAX_TARGETS of them anyway.
MAX_DISCOVERED_ENDPOINTS = 50
# Main-frame document responses kept for the passive-security check.
MAX_DOCUMENT_RESPONSES = 50
# How much of an error page is read for the stack-trace rule.
_BODY_SAMPLE_MAX_CHARS = 4000

# Navigation Timing + paint entries, read out of the page already loaded.
# Everything here is a number the browser recorded during the load we
# already performed; nothing triggers a second one.
_PERFORMANCE_SCRIPT = """() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const paints = {};
  for (const entry of performance.getEntriesByType('paint')) {
    paints[entry.name] = Math.round(entry.startTime);
  }
  const resources = performance.getEntriesByType('resource');
  return {
    ttfb_ms: nav ? Math.round(nav.responseStart) : null,
    dom_content_loaded_ms: nav ? Math.round(nav.domContentLoadedEventEnd) : null,
    load_ms: nav ? Math.round(nav.loadEventEnd) : null,
    transfer_bytes: nav ? nav.transferSize : null,
    first_paint_ms: paints['first-paint'] ?? null,
    first_contentful_paint_ms: paints['first-contentful-paint'] ?? null,
    resource_count: resources.length,
    resource_bytes: resources.reduce((total, r) => total + (r.transferSize || 0), 0)
  };
}"""

_FINDING_LIMIT = (
    "ERROR: this session has reached its limit of {limit} findings. "
    "Continue exploring or call finish_session, but record no more."
)


@dataclass
class FindingRecord:
    """One finding, handed to ``on_finding`` alongside its screenshot bytes."""

    finding_type: str
    severity: str
    title: str
    steps_to_reproduce: str
    expected: str
    actual: str
    # Browser, viewport, OS, and page URL at the moment of recording.
    # Optional so a session assembled without a live browser still records.
    environment: str | None = None


@dataclass
class CheckOutcome:
    """One catalogue check's result, or why it could not produce one.

    A third state alongside "found something" and "found nothing": a check
    that could not run has told us nothing about the page, and recording
    that as clean is the one reading that is actually false.  The caller
    turns ``error`` into a ``failed_to_run`` outcome on the target row.
    """

    data: Any = None
    error: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


def allowed_origins(urls: list[str]) -> set[tuple[str, str]]:
    """Reduce nominated URLs to the (scheme, netloc) pairs the lock allows."""
    origins: set[tuple[str, str]] = set()
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            origins.add((parsed.scheme, parsed.netloc))
    return origins


class BrowserSession:
    """One live browser for one charter, exposing the exploratory tool set.

    Use as a context manager — the browser is always closed on exit, even
    when an executor failed internally.
    """

    def __init__(
        self,
        base_urls: list[str],
        env_vars: dict[str, str],
        on_finding: Callable[[FindingRecord, bytes | None], str | None],
        headless: bool | None = None,
        action_timeout: int | None = None,
        max_findings: int | None = None,
        on_navigated: Callable[[str, float], str] | None = None,
        catalogue_timeout: int | None = None,
    ) -> None:
        self._origins = allowed_origins(base_urls)
        # Kept alongside _origins because order matters and a set has none:
        # __enter__ opens the session on the first URL, which charter
        # generation is instructed to make the browsable frontend.
        self._base_urls = list(base_urls)
        self._env_vars = env_vars
        self._on_finding = on_finding
        self._headless = EXPLORATORY_HEADLESS if headless is None else headless
        self._action_timeout = (
            EXPLORATORY_ACTION_TIMEOUT if action_timeout is None else action_timeout
        )
        self._max_findings = EXPLORATORY_MAX_FINDINGS if max_findings is None else max_findings
        # Fired after any executor that can change the URL — see
        # `_fire_navigated`. None for an exploratory session, which has no
        # catalogue to run.
        self._on_navigated = on_navigated
        self._catalogue_timeout = (
            NONFUNCTIONAL_CATALOGUE_TIMEOUT if catalogue_timeout is None else catalogue_timeout
        )

        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._console: list[str] = []
        # XHR/fetch URLs the application called for itself. Deliberately NOT
        # in `_console`: `read_console` drains and clears that list, and a
        # discovery set the task reads after the loop cannot live somewhere
        # the model can empty.
        self.discovered_endpoints: list[str] = []
        # Main-frame document responses, by URL — captured as they arrive so
        # the passive-security check reads the response the *browser* got
        # rather than issuing a second request that a server could answer
        # differently.
        self._document_responses: dict[str, dict] = {}
        self.findings_recorded = 0
        # Set once in __enter__. Initialized here because this class is also
        # constructed directly with a page injected (tests do exactly that),
        # and every finding path must survive the browser never existing.
        self._browser_label: str | None = None

    # ── lifecycle ─────────────────────────────────────────────────────

    def __enter__(self) -> BrowserSession:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        # Read once: a browser cannot change version mid-session, and the
        # alternative is doing this work on the failure path, at the worst
        # possible moment. Never fatal — a finding with a vaguer environment
        # beats no session.
        try:
            self._browser_label = (
                f"{self._browser.browser_type.name.capitalize()} {self._browser.version}"
            )
        except Exception:  # pragma: no cover — driver-dependent
            logger.warning("Could not read the browser version", exc_info=True)
        self._page = self._browser.new_page()
        self._page.set_default_timeout(self._action_timeout * 1000)
        self._page.on("console", self._record_console)
        self._page.on("requestfailed", self._record_request_failure)
        # A separate handler, not a widening of `_record_request_failure`:
        # that one is bound to "requestfailed" and therefore only ever sees
        # requests that *failed*, which is precisely the traffic this is not
        # interested in.
        self._page.on("request", self._record_request)
        self._page.on("response", self._record_response)

        # Open on the application rather than about:blank. Without this the
        # model's first action is spent discovering where the app lives.
        # Guarded because BrowserSession is constructible directly: an
        # IndexError here would escape the never-raise contract and kill a
        # session before any executor ran.
        if self._base_urls:
            result = self.navigate(self._base_urls[0])
            if result.startswith("ERROR:"):
                # Deliberately not fatal — an unreachable application is
                # itself a finding the model should get to record.
                logger.warning("Could not open the application under test: %s", result)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for closer in (self._browser, self._playwright):
            if closer is None:
                continue
            try:
                closer.close() if closer is self._browser else closer.stop()
            except Exception:  # pragma: no cover — teardown must never mask
                logger.warning("Browser session teardown failed", exc_info=True)

    def _record_console(self, message: Any) -> None:
        if getattr(message, "type", None) in ("error", "warning"):
            self._console.append(f"console.{message.type}: {message.text}")

    def _record_request_failure(self, request: Any) -> None:
        failure = getattr(request, "failure", None)
        self._console.append(f"request failed: {request.url} ({failure})")

    def _record_request(self, request: Any) -> None:
        """Remember an XHR/fetch URL the application called for itself.

        These are the endpoint targets — URLs the application uses, not
        anything guessed or crawled.  Off-origin traffic is skipped: an
        analytics beacon is not part of the software under test, and
        examining it would put our requests on a third party's API.

        Never raises: it runs inside Playwright's event dispatch, where an
        exception has no caller of ours to catch it.
        """
        try:
            if getattr(request, "resource_type", None) not in ("xhr", "fetch"):
                return
            url = str(request.url).split("#", 1)[0]
            if not self._is_allowed(url) or url in self.discovered_endpoints:
                return
            if len(self.discovered_endpoints) >= MAX_DISCOVERED_ENDPOINTS:
                return
            self.discovered_endpoints.append(url)
        except Exception:  # pragma: no cover - event handlers have no caller
            logger.debug("Could not record a request URL", exc_info=True)

    def _record_response(self, response: Any) -> None:
        """Keep the main-frame document response for the passive checks."""
        try:
            request = response.request
            if getattr(request, "resource_type", None) != "document":
                return
            is_navigation = getattr(request, "is_navigation_request", None)
            if callable(is_navigation) and not is_navigation():
                return
            url = str(response.url).split("#", 1)[0]
            self._document_responses[url] = {
                "status": response.status,
                "headers": dict(response.headers or {}),
            }
            if len(self._document_responses) > MAX_DOCUMENT_RESPONSES:
                # Oldest first: a run walks forward, so the page we are on is
                # always among the newest.
                for stale in list(self._document_responses)[:-MAX_DOCUMENT_RESPONSES]:
                    self._document_responses.pop(stale, None)
        except Exception:  # pragma: no cover - event handlers have no caller
            logger.debug("Could not record a document response", exc_info=True)

    # ── the navigation hook ───────────────────────────────────────────

    def _fire_navigated(self, previous_url: str | None) -> str:
        """Run the arrival callback when an executor changed the URL.

        The catalogue runs here rather than behind a tool the model calls,
        because a tool the model calls is a tool the model can decline to
        call — and "the full catalogue at every target" would quietly become
        "wherever it remembered to look".

        Wrapped in its own ``try`` because this hook widens the never-raise
        contract further than any other executor: after it, ``click()``
        transitively performs an axe injection, a screenshot, a disk write
        and several database commits.  A full disk must cost the target row,
        never the session — a session that dies here loses every target
        after it, and a ``failed_to_run`` outcome would have survived.
        """
        if self._on_navigated is None:
            return ""
        try:
            current = self._page.url
        except PlaywrightError:
            return ""
        if previous_url is not None and current == previous_url:
            return ""

        started = time.monotonic()
        # A synchronous callback on this thread cannot be preempted — the
        # browser lives here and cannot be driven from another thread — so
        # the budget is a deadline the callback honours between checks.
        deadline = started + self._catalogue_timeout
        try:
            note = self._on_navigated(current, deadline)
        except Exception:
            logger.exception("The arrival callback failed for %s", current)
            return ""
        elapsed = time.monotonic() - started
        if elapsed > self._catalogue_timeout:
            logger.warning(
                "Arrival callback for %s took %.1fs, over its %ds budget",
                current,
                elapsed,
                self._catalogue_timeout,
            )
        return "\n" + note if note else ""

    # ── helpers ───────────────────────────────────────────────────────

    def _locator(self, ref: str):
        return self._page.locator(f"aria-ref={ref}")

    def _page_header(self) -> str:
        """URL + title, prefixed to every snapshot.

        An SPA can navigate without the model noticing otherwise — the URL is
        often the only signal that a click moved somewhere.
        """
        try:
            header = f"Current URL: {self._page.url}\nPage title: {self._page.title()}"
        except PlaywrightError:
            return ""
        return f"{header}{self._off_origin_notice()}\n\n"

    def _off_origin_notice(self) -> str:
        """Warn when the page has left the application under test.

        Deliberately a notice, not a block: the application's own links may
        legitimately lead off-origin (an OAuth hop, a payment provider, an
        external reference the charter asks us to verify), and following one
        can be exactly what a charter calls for.  Undoing it would remove real
        testing capability and could manufacture a "the link is broken"
        finding about a link that works.

        What must not happen is the model exploring a third-party site
        *without realising it* and filing findings against software that is
        not under test — so this rides along on every snapshot header rather
        than firing once at the moment of the click.

        Typed navigation stays hard-locked in ``navigate``: that is the agent
        choosing to leave the application. This covers the application taking
        it somewhere, which is the application's own behaviour.
        """
        try:
            url = self._page.url
        except PlaywrightError:
            return ""
        if self._is_allowed(url):
            return ""
        return (
            f"\nNOTE: this page is outside the application under test "
            f"({self._origins_text()}). Anything wrong here is someone else's "
            "software unless the charter says otherwise — navigate back to the "
            "application when you are done."
        )

    def _current_url(self) -> str | None:
        """The page's URL, or None when the page cannot answer for it."""
        try:
            return self._page.url
        except PlaywrightError:
            return None

    def _is_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        return (parsed.scheme, parsed.netloc) in self._origins

    def _origins_text(self) -> str:
        return ", ".join(f"{scheme}://{netloc}" for scheme, netloc in sorted(self._origins))

    # ── tool executors ────────────────────────────────────────────────

    def snapshot(self) -> str:
        try:
            tree = self._page.locator("body").aria_snapshot(mode="ai")
        except PlaywrightError as exc:
            return f"ERROR: could not snapshot the page: {exc}"
        if len(tree) > EXPLORATORY_SNAPSHOT_MAX_CHARS:
            tree = tree[:EXPLORATORY_SNAPSHOT_MAX_CHARS] + _SNAPSHOT_TRUNCATION_MARKER
        return self._page_header() + tree

    def navigate(self, url: str = "") -> str:
        """Go to a URL, hard-locked to the nominated origins.

        Typed navigation is refused off-origin because it is the agent
        choosing to leave the application under test.  Link-driven navigation
        is *not* refused — see ``_off_origin_notice`` for why the two are
        treated differently.
        """
        if not self._is_allowed(url):
            return (
                f"ERROR: navigation to {url!r} is not allowed. This session may "
                f"only visit the application under test: {self._origins_text()}."
            )
        before = self._current_url()
        try:
            self._page.goto(url)
        except PlaywrightError as exc:
            return f"ERROR: could not navigate to {url!r}: {exc}"

        # Re-check after the navigation settles: a redirect chain can land
        # somewhere the requested URL alone wouldn't have revealed.
        landed = self._page.url
        if not self._is_allowed(landed):
            with contextlib.suppress(PlaywrightError):
                self._page.go_back()
            return (
                f"ERROR: {url!r} redirected to {landed!r}, which is outside the "
                f"application under test ({self._origins_text()}). Navigation was undone."
            )
        # Ordering is load-bearing: the hook fires only *after* the
        # post-settle origin check above. Firing it first would run the whole
        # catalogue — axe injection included — against a third-party site the
        # application merely redirected to.
        return f"Navigated to {landed}. Call snapshot to see the page." + self._fire_navigated(
            before
        )

    def click(self, ref: str = "") -> str:
        before = self._current_url()
        try:
            self._locator(ref).click()
        except PlaywrightError as exc:
            return self._element_error(ref, exc, "click")
        return (
            f"Clicked {ref}. The page may have changed — take a fresh snapshot."
            + self._off_origin_notice()
            + self._fire_navigated(before)
        )

    def fill(self, ref: str = "", value: str = "") -> str:
        try:
            self._locator(ref).fill(value)
        except PlaywrightError as exc:
            return self._element_error(ref, exc, "fill")
        return f"Filled {ref}."

    def fill_secret(self, ref: str = "", env_var_name: str = "") -> str:
        """Type a secret without its literal ever entering the conversation.

        This is what keeps the stored action log credential-free by
        construction rather than by after-the-fact redaction — the value is
        resolved here and never returned to the model.
        """
        if env_var_name not in self._env_vars:
            available = ", ".join(sorted(self._env_vars)) or "(none)"
            return f"ERROR: no environment variable named {env_var_name!r}. Available: {available}."
        try:
            self._locator(ref).fill(self._env_vars[env_var_name])
        except PlaywrightError as exc:
            return self._element_error(ref, exc, "fill")
        return f"Filled {ref} with the value of {env_var_name}."

    def press(self, ref: str = "", key: str = "") -> str:
        before = self._current_url()
        try:
            self._locator(ref).press(key)
        except PlaywrightError as exc:
            return self._element_error(ref, exc, "press")
        return (
            f"Pressed {key} on {ref}. The page may have changed — take a fresh snapshot."
            + self._off_origin_notice()
            + self._fire_navigated(before)
        )

    def go_back(self) -> str:
        before = self._current_url()
        try:
            self._page.go_back()
        except PlaywrightError as exc:
            return f"ERROR: could not go back: {exc}"
        return (
            f"Went back to {self._page.url}. Take a fresh snapshot."
            + self._off_origin_notice()
            + self._fire_navigated(before)
        )

    def go_forward(self) -> str:
        before = self._current_url()
        try:
            self._page.go_forward()
        except PlaywrightError as exc:
            return f"ERROR: could not go forward: {exc}"
        return (
            f"Went forward to {self._page.url}. Take a fresh snapshot."
            + self._off_origin_notice()
            + self._fire_navigated(before)
        )

    def set_viewport(self, width: int = 1280, height: int = 720) -> str:
        try:
            self._page.set_viewport_size({"width": int(width), "height": int(height)})
        except (PlaywrightError, TypeError, ValueError) as exc:
            return f"ERROR: could not resize viewport: {exc}"
        return f"Viewport is now {width}x{height}. Take a fresh snapshot."

    def read_console(self) -> str:
        if not self._console:
            return "No console errors or failed requests since the last check."
        drained = "\n".join(self._console)
        self._console.clear()
        return drained

    def record_finding(
        self,
        finding_type: str = "bug",
        severity: str = "medium",
        title: str = "",
        steps_to_reproduce: str = "",
        expected: str = "",
        actual: str = "",
        page_still_shows_problem: bool = True,
    ) -> str:
        if self.findings_recorded >= self._max_findings:
            return _FINDING_LIMIT.format(limit=self._max_findings)

        # Capture while the problem is still on screen — this is the whole
        # reason recording is a tool rather than a post-hoc parse of the notes.
        #
        # When it isn't, skip: an image of whatever the model happened to be
        # looking at reads as evidence of the defect, and false evidence is
        # worse than none. The caller reports the fact ("is it still visible")
        # rather than the action, so this decision stays in code.
        screenshot: bytes | None = None
        if page_still_shows_problem:
            try:
                screenshot = self._page.screenshot()
            except PlaywrightError as exc:
                logger.warning("Finding screenshot failed: %s", exc)

        record = FindingRecord(
            finding_type=finding_type,
            severity=severity,
            title=title,
            steps_to_reproduce=steps_to_reproduce,
            expected=expected,
            actual=actual,
            # Same instant as the screenshot, for the same reason: the
            # viewport and URL describe *this* observation, and a later
            # action would silently change both.
            environment=self._environment(),
        )
        try:
            self._on_finding(record, screenshot)
        except Exception as exc:  # persistence must not kill the session
            logger.exception("Recording a finding failed")
            return f"ERROR: the finding could not be saved: {exc}"

        self.findings_recorded += 1
        return f"Recorded {finding_type}: {title}. Continue exploring the charter."

    def _environment(self) -> str:
        """Describe where this observation was made.

        Reads the page defensively: a page that has crashed or closed
        answers for neither its URL nor its viewport, and that must cost the
        detail, never the finding.
        """
        url: str | None = None
        viewport: dict[str, int] | None = None
        with contextlib.suppress(PlaywrightError, AttributeError):
            url = self._page.url
        with contextlib.suppress(PlaywrightError, AttributeError):
            viewport = self._page.viewport_size
        return environment_utils.browser_environment(self._browser_label, viewport, url)

    # ── the catalogue (nonfunctional runs) ────────────────────────────
    #
    # None of these is a tool. The model is never offered them, because a
    # check it can call is a check it can decline to call — and the whole
    # claim of this run mode is that the full catalogue runs at every
    # target. They are called by the arrival callback instead.

    def scan_accessibility(self) -> CheckOutcome:
        """Run axe-core against the current page.

        ``axe-playwright-python`` injects through ``page.evaluate``, so page
        CSP is not a failure mode here — verified against 0.1.8.  What does
        fail is the evaluate itself: a page navigating under the call, a
        detached frame, JavaScript disabled.

        Known limit, and not detectable from here: axe analyses the **main
        frame only**, so a violation inside a cross-origin iframe reads
        exactly like a clean frame.
        """
        try:
            from axe_playwright_python.sync_playwright import Axe
        except ImportError as exc:  # pragma: no cover - dependency is declared
            return CheckOutcome(error=f"axe is not installed: {exc}")
        try:
            return CheckOutcome(data=Axe().run(self._page))
        except Exception as exc:
            return CheckOutcome(error=f"axe could not run on this page: {exc}")

    def document_content_type(self) -> str | None:
        """The media type the browser was served for the current page.

        Read off the captured document response, never re-requested, and
        never inferred from the DOM: Chromium renders an
        ``application/json`` body through its own JSON viewer, so the page
        axe sees is real HTML whatever the server said.  The header is the
        only thing that can tell a page from a payload.

        ``None`` when the URL, the record or the header is missing — an
        in-page SPA navigation captures no document response, and the
        caller must read that as *unknown*, never as "not a page".
        """
        url = self._current_url()
        if url is None:
            return None
        recorded = self._document_responses.get(url.split("#", 1)[0])
        if recorded is None:
            return None
        header = (recorded.get("headers") or {}).get("content-type")
        if not header:
            return None
        return str(header).split(";", 1)[0].strip().lower() or None

    def check_headers(self) -> CheckOutcome:
        """The response the browser actually got for the current page.

        Read from the captured document response rather than re-requested:
        a second request is a second chance for the server to answer
        differently, and the passive checks are supposed to describe what a
        real visitor received.
        """
        url = self._current_url()
        if url is None:
            return CheckOutcome(error="the page could not be read")
        recorded = self._document_responses.get(url.split("#", 1)[0])
        if recorded is None:
            return CheckOutcome(
                error=f"no document response was captured for {url} (an in-page navigation?)"
            )
        cookies: list[dict] = []
        with contextlib.suppress(Exception):
            cookies = list(self._page.context.cookies())
        body_sample = ""
        if recorded["status"] >= 400:
            # Only for an error response: the stack-trace rule is the one
            # check that reads a body, and a 200 page about tracebacks is
            # not a leaking error response.
            with contextlib.suppress(Exception):
                body_sample = (self._page.content() or "")[:_BODY_SAMPLE_MAX_CHARS]
        return CheckOutcome(
            data={
                "url": url,
                "status": recorded["status"],
                "headers": recorded["headers"],
                # Names and flags only — a cookie value is never returned to
                # a caller, logged, or rendered into a finding.
                "cookies": [
                    {
                        "name": cookie.get("name"),
                        "secure": cookie.get("secure"),
                        "httpOnly": cookie.get("httpOnly"),
                        "sameSite": cookie.get("sameSite"),
                    }
                    for cookie in cookies
                ],
                "body_sample": body_sample,
            }
        )

    def measure_performance(self) -> CheckOutcome:
        """Timings for the page already loaded — no second page load.

        Navigation Timing plus the paint entries, read out of the page that
        is already there.  Deliberately not LCP or CLS: both need a
        ``PerformanceObserver`` running from before navigation and settle
        only after user-visible delay, which would mean loading the page a
        second time to measure it.  Data only, either way — decision 11
        keeps performance out of findings entirely.
        """
        try:
            data = self._page.evaluate(_PERFORMANCE_SCRIPT)
        except Exception as exc:
            return CheckOutcome(error=f"performance timings could not be read: {exc}")
        if not isinstance(data, dict):
            return CheckOutcome(error="performance timings came back in an unexpected shape")
        return CheckOutcome(data=data)

    def cookies_for_load(self) -> dict[str, str]:
        """The browser's cookies, for a load profile to send.

        Called on the **main thread** and passed into the thread pool by
        value: no Playwright handle ever crosses that boundary, because the
        sync API belongs to the thread that opened it.

        Values are returned — they have to be, the requests carry them — but
        never logged here and never persisted by the caller.
        """
        try:
            return {
                str(cookie["name"]): str(cookie.get("value", ""))
                for cookie in self._page.context.cookies()
                if cookie.get("name")
            }
        except Exception:
            logger.warning("Could not read cookies for a load profile", exc_info=True)
            return {}

    def screenshot(self) -> bytes | None:
        """One page image, or None. Never raises — evidence is optional."""
        try:
            return self._page.screenshot()
        except Exception as exc:
            logger.warning("Page screenshot failed: %s", exc)
            return None

    def _element_error(self, ref: str, exc: PlaywrightError, action: str) -> str:
        message = str(exc)
        if "Timeout" in message or "strict mode violation" in message:
            return _STALE_REF.format(ref=ref)
        return f"ERROR: could not {action} {ref!r}: {message}"

    # ── registry ──────────────────────────────────────────────────────

    def tool_registry(self) -> dict[str, Callable[..., str]]:
        """Map tool name to executor, as ``run_exploration_loop`` expects.

        ``finish_session`` is absent deliberately: it is terminal and handled
        by the loop itself, never dispatched to the browser.
        """
        return {
            "snapshot": self.snapshot,
            "navigate": self.navigate,
            "click": self.click,
            "fill": self.fill,
            "fill_secret": self.fill_secret,
            "press": self.press,
            "go_back": self.go_back,
            "go_forward": self.go_forward,
            "set_viewport": self.set_viewport,
            "read_console": self.read_console,
            "record_finding": self.record_finding,
        }

    def nonfunctional_tool_registry(self) -> dict[str, Callable[..., str]]:
        """Navigation only — the executor half of the inverted oracle.

        ``record_finding`` is absent, but note what that does and does not
        buy: this dict *dispatches* a call the model already made, so
        omitting an entry makes the tool fail loudly rather than making it
        unavailable.  The guarantee lives in the request **schema**
        (``NONFUNCTIONAL_TOOLS`` in ``llm_prompts.py``), which is what
        decides whether the model is offered the tool at all.  This is a
        cheap second layer, not the pin.
        """
        navigation_only = self.tool_registry()
        navigation_only.pop("record_finding", None)
        return navigation_only
