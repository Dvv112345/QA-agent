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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from backend.config import (
    EXPLORATORY_ACTION_TIMEOUT,
    EXPLORATORY_HEADLESS,
    EXPLORATORY_MAX_FINDINGS,
    EXPLORATORY_SNAPSHOT_MAX_CHARS,
)

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

        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._console: list[str] = []
        self.findings_recorded = 0

    # ── lifecycle ─────────────────────────────────────────────────────

    def __enter__(self) -> BrowserSession:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        self._page = self._browser.new_page()
        self._page.set_default_timeout(self._action_timeout * 1000)
        self._page.on("console", self._record_console)
        self._page.on("requestfailed", self._record_request_failure)

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
        return f"Navigated to {landed}. Call snapshot to see the page."

    def click(self, ref: str = "") -> str:
        try:
            self._locator(ref).click()
        except PlaywrightError as exc:
            return self._element_error(ref, exc, "click")
        return (
            f"Clicked {ref}. The page may have changed — take a fresh snapshot."
            + self._off_origin_notice()
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
        try:
            self._locator(ref).press(key)
        except PlaywrightError as exc:
            return self._element_error(ref, exc, "press")
        return (
            f"Pressed {key} on {ref}. The page may have changed — take a fresh snapshot."
            + self._off_origin_notice()
        )

    def go_back(self) -> str:
        try:
            self._page.go_back()
        except PlaywrightError as exc:
            return f"ERROR: could not go back: {exc}"
        return f"Went back to {self._page.url}. Take a fresh snapshot." + self._off_origin_notice()

    def go_forward(self) -> str:
        try:
            self._page.go_forward()
        except PlaywrightError as exc:
            return f"ERROR: could not go forward: {exc}"
        return (
            f"Went forward to {self._page.url}. Take a fresh snapshot." + self._off_origin_notice()
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
    ) -> str:
        if self.findings_recorded >= self._max_findings:
            return _FINDING_LIMIT.format(limit=self._max_findings)

        # Capture while the problem is still on screen — this is the whole
        # reason recording is a tool rather than a post-hoc parse of the notes.
        screenshot: bytes | None = None
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
        )
        try:
            self._on_finding(record, screenshot)
        except Exception as exc:  # persistence must not kill the session
            logger.exception("Recording a finding failed")
            return f"ERROR: the finding could not be saved: {exc}"

        self.findings_recorded += 1
        return f"Recorded {finding_type}: {title}. Continue exploring the charter."

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
