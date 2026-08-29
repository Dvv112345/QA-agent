"""Turn raw tool output into violations — the nonfunctional oracle.

This module is where a nonfunctional run decides **what is wrong and how
badly**, and it is deliberately the only place that decides it.  Both
sources are deterministic: axe-core answers for accessibility, and a fixed
table below answers for passive security.  No model is consulted, which is
why a run's findings mean the same thing on Tuesday as on Monday.

The LLM's whole job downstream is prose — a readable title and steps — so
every violation leaves here **already carrying complete finding text**.
That is not belt-and-braces.  ``services/findings.py`` drops any finding
with no ``title``, so a triage outage without this fallback would silently
discard real violations rather than merely leaving them badly worded.

Everything here is pure.  The one piece of run-scoped state a run needs —
the ``(rule, url)`` de-duplication across targets — is the
``RunViolations`` accumulator, which the *task* owns and passes in, so its
lifetime across a restart is visible where the restart is.

Two things about axe worth knowing before changing anything (verified
against ``axe-playwright-python`` 0.1.8, axe-core 4.12.1):

* It injects through ``page.evaluate``, not ``add_script_tag`` — so page
  CSP does **not** apply and a strict-CSP page is not a failure mode.  What
  does fail is ``page.evaluate`` itself (a page navigating under the call,
  a detached frame), and that is the caller's ``failed_to_run``.
* It analyses the **main frame only**.  A violation inside a cross-origin
  iframe is not reported, and reads here exactly like a clean frame.  A
  known limit, not something this module can detect.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from backend.config import NONFUNCTIONAL_AXE_MAX_CHARS
from backend.models.database import (
    FindingSeverity,
    FindingType,
    NonfunctionalDomain,
)

logger = logging.getLogger(__name__)


class CheckError(Exception):
    """A check could not be run, or its output was not what the tool promises.

    Raised rather than returning an empty list, because "no violations" and
    "the parse failed" are the two readings that must never be confused: an
    axe-core upgrade that changed the payload shape would otherwise report
    every page clean forever, under an append-only defect table and a
    monotonic bug count — silent, and permanent.
    """


# How many affected elements one violation lists before the rest are
# summarised as a count. A page can break one rule in hundreds of places
# and the finding still has to be readable.
MAX_NODES_PER_VIOLATION = 10
# Longest single node snippet kept.
MAX_NODE_CHARS = 300

# axe `impact` → our severity. Two axe levels collapse into `high`
# because the split between them is about how many users are blocked, and
# both are "fix this".
_IMPACT_SEVERITY = {
    "critical": FindingSeverity.HIGH,
    "serious": FindingSeverity.HIGH,
    "moderate": FindingSeverity.MEDIUM,
    "minor": FindingSeverity.LOW,
}

# A violation is reportable when it breaks a WCAG success criterion. axe
# also ships best-practice and experimental rules, which are advice rather
# than a standard — they are kept as data (decision 25) so the run can say
# what it saw, and they never become findings a team is asked to triage.
_WCAG_TAG = re.compile(r"^wcag\d")


@dataclass(frozen=True)
class RawViolation:
    """One rule broken at one URL, with every field a finding needs.

    ``nodes`` lists the affected elements *inside* one violation rather
    than exploding into one finding per element (decision 8): twenty
    unlabelled buttons on a page are one defect with twenty occurrences,
    and filing them as twenty tickets is how a real accessibility report
    becomes unreadable.

    The four text fields are the deterministic fallback.  Triage overwrites
    the prose and nothing else — never ``severity``, never ``rule``.
    """

    domain: str
    rule: str
    url: str
    severity: str
    summary: str
    nodes: tuple[str, ...] = ()
    title: str = ""
    steps_to_reproduce: str = ""
    expected: str = ""
    actual: str = ""
    help_url: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        """Identity for de-duplication: one finding per rule per URL."""
        return (self.domain, self.rule, self.url)

    @property
    def finding_type(self) -> str:
        """Always a bug.

        ``issue`` means the testing itself was obstructed, which here is
        recorded as a domain *outcome* on the target row rather than as a
        finding — a check that could not run has nothing to report about
        the product.
        """
        return FindingType.BUG


@dataclass
class AxeScan:
    """One page's accessibility result, split by what it obliges.

    ``advisory`` is the best-practice and experimental half: recorded on
    the target as data, never filed. Keeping them in the same return value
    is what makes "we looked and found only advisory items" expressible —
    which is a different statement from "clean".
    """

    violations: list[RawViolation] = field(default_factory=list)
    advisory: list[dict] = field(default_factory=list)
    engine_version: str = ""


# ── Accessibility ─────────────────────────────────────────────────────


def _payload(result: Any) -> dict:
    """The raw dict, whether given an ``AxeResults`` or the dict itself.

    ``axe-playwright-python`` returns an object wrapping the response, so
    a signature that claimed ``dict`` would be a lie the first caller
    discovers at runtime.
    """
    payload = getattr(result, "response", result)
    if not isinstance(payload, dict):
        raise CheckError(f"axe returned {type(payload).__name__}, not a result payload")
    return payload


def _node_description(node: Any) -> str:
    """One affected element, as a reader can act on it: selector + snippet."""
    if not isinstance(node, dict):
        raise CheckError("axe violation node is not an object")
    target = node.get("target") or []
    selector = ", ".join(str(part) for part in target) if isinstance(target, list) else str(target)
    snippet = (node.get("html") or "").replace("\n", " ").strip()
    if len(snippet) > MAX_NODE_CHARS:
        snippet = snippet[:MAX_NODE_CHARS] + "…"
    return f"{selector or '(unknown element)'} — {snippet}" if snippet else selector


def _fallback_text(
    *, rule: str, url: str, description: str, help_text: str, nodes: tuple[str, ...]
) -> dict[str, str]:
    """Complete finding text derived from the rule and the URL alone.

    Deterministic on purpose: this is what a finding says when triage never
    ran, and it has to be good enough to act on rather than a placeholder.
    """
    count = len(nodes)
    listed = "\n".join(f"- {node}" for node in nodes)
    return {
        "title": f"{description or rule} ({rule})",
        "steps_to_reproduce": f"Open {url}\nInspect the affected elements:\n{listed}".strip(),
        "expected": help_text or f"The page satisfies the {rule} accessibility rule.",
        "actual": f"{count} element(s) on this page break {rule}.",
    }


def axe_violations(result: Any, url: str) -> AxeScan:
    """Parse one axe run into violations plus advisory items.

    Raises :class:`CheckError` on anything that is not the shape axe
    promises — a missing ``violations`` key, a violation that is not an
    object, nodes that are not a list.  The caller records
    ``failed_to_run``; it must never record ``clean``.
    """
    payload = _payload(result)
    raw = payload.get("violations")
    if not isinstance(raw, list):
        raise CheckError("axe result carries no `violations` list")

    engine = payload.get("testEngine") or {}
    scan = AxeScan(
        engine_version=str(engine.get("version", "")) if isinstance(engine, dict) else ""
    )

    for violation in raw:
        if not isinstance(violation, dict):
            raise CheckError("axe violation is not an object")
        rule = violation.get("id")
        if not rule:
            raise CheckError("axe violation carries no rule id")
        nodes_raw = violation.get("nodes")
        if not isinstance(nodes_raw, list):
            raise CheckError(f"axe violation {rule} carries no `nodes` list")

        tags = violation.get("tags") or []
        descriptions = tuple(
            _node_description(node) for node in nodes_raw[:MAX_NODES_PER_VIOLATION]
        )
        if len(nodes_raw) > MAX_NODES_PER_VIOLATION:
            descriptions += (f"… and {len(nodes_raw) - MAX_NODES_PER_VIOLATION} more element(s)",)

        if not any(_WCAG_TAG.match(str(tag)) for tag in tags):
            # Advisory only — recorded so the run can report it, never filed.
            scan.advisory.append(
                {
                    "rule": rule,
                    "impact": violation.get("impact"),
                    "node_count": len(nodes_raw),
                    "tags": [str(tag) for tag in tags],
                }
            )
            continue

        # An unknown impact defaults to medium rather than being dropped:
        # axe found something, and the severity is the part we are unsure
        # of, not the violation.
        severity = _IMPACT_SEVERITY.get(
            str(violation.get("impact") or "").lower(), FindingSeverity.MEDIUM
        )
        text = _fallback_text(
            rule=rule,
            url=url,
            description=str(violation.get("description") or ""),
            help_text=str(violation.get("help") or ""),
            nodes=descriptions,
        )
        scan.violations.append(
            RawViolation(
                domain=NonfunctionalDomain.ACCESSIBILITY,
                rule=str(rule),
                url=url,
                severity=severity,
                summary=str(violation.get("description") or rule),
                nodes=descriptions,
                help_url=str(violation.get("helpUrl") or ""),
                **text,
            )
        )
    return scan


def capped_axe_payload(result: Any, max_chars: int = NONFUNCTIONAL_AXE_MAX_CHARS) -> str:
    """The axe payload as text, truncated before it can reach a prompt.

    Never raises: this feeds context, not correctness, and a payload too
    strange to serialize is still not a reason to fail a target.
    """
    try:
        text = json.dumps(_payload(result))
    except (CheckError, TypeError, ValueError):
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…[truncated]"


# ── Passive security ──────────────────────────────────────────────────
#
# Passive means exactly what it says: everything below is read off a
# response the application was going to send anyway. Nothing here probes,
# fuzzes, or sends a crafted request — that was cut from the feature on
# purpose, and a rule that needs an attack to confirm does not belong in
# this table.

# Six months, the value the HSTS preload list requires.
_HSTS_MIN_MAX_AGE = 15552000

_STACK_TRACE_MARKERS = (
    "Traceback (most recent call last)",
    "java.lang.",
    "at System.",
    "Exception in thread",
    "<b>Fatal error</b>",
    "ORA-0",
    "SQLSTATE[",
)

_SECURITY_TEXT = {
    "missing-hsts": (
        "HTTPS responses do not ask the browser to stay on HTTPS",
        "The response carries a Strict-Transport-Security header with a max-age of at "
        f"least {_HSTS_MIN_MAX_AGE} seconds.",
    ),
    "weak-hsts": (
        "Strict-Transport-Security expires too soon to protect a returning visitor",
        f"Strict-Transport-Security carries a max-age of at least {_HSTS_MIN_MAX_AGE} seconds.",
    ),
    "missing-csp": (
        "No Content-Security-Policy is sent",
        "The response carries a Content-Security-Policy header.",
    ),
    "missing-x-content-type-options": (
        "The browser is allowed to guess content types",
        "The response carries X-Content-Type-Options: nosniff.",
    ),
    "missing-referrer-policy": (
        "No Referrer-Policy is sent",
        "The response carries a Referrer-Policy header.",
    ),
    "cookie-missing-secure": (
        "A cookie may be sent over plain HTTP",
        "Every cookie set over HTTPS carries the Secure attribute.",
    ),
    "cookie-missing-httponly": (
        "A cookie is readable by page scripts",
        "Session cookies carry the HttpOnly attribute.",
    ),
    "cookie-missing-samesite": (
        "A cookie has no SameSite attribute",
        "Cookies declare a SameSite attribute rather than relying on the browser default.",
    ),
    "server-header-disclosure": (
        "The server names its own software and version",
        "The Server header does not disclose a version.",
    ),
    "x-powered-by-disclosure": (
        "The response advertises the application stack",
        "No X-Powered-By header is sent.",
    ),
    "error-response-leaks-a-stack-trace": (
        "An error response contains a stack trace",
        "Error responses carry a message, never internal frames or SQL state.",
    ),
}


def _security_violation(
    rule: str, url: str, severity: str, actual: str, evidence: tuple[str, ...] = ()
) -> RawViolation:
    title, expected = _SECURITY_TEXT[rule]
    return RawViolation(
        domain=NonfunctionalDomain.SECURITY,
        rule=rule,
        url=url,
        severity=severity,
        summary=title,
        nodes=evidence,
        title=f"{title} ({rule})",
        steps_to_reproduce=f"Request {url}\nRead the response headers",
        expected=expected,
        actual=actual,
    )


def _hsts_violations(url: str, value: str | None) -> list[RawViolation]:
    if not url.lower().startswith("https://"):
        # Plain HTTP is a finding of its own kind, not an HSTS one — and a
        # local http:// test environment is a deliberate choice, not a bug.
        return []
    if value is None:
        return [
            _security_violation(
                "missing-hsts", url, FindingSeverity.MEDIUM, "No Strict-Transport-Security header."
            )
        ]
    match = re.search(r"max-age\s*=\s*(\d+)", value, re.IGNORECASE)
    max_age = int(match.group(1)) if match else 0
    if max_age < _HSTS_MIN_MAX_AGE:
        return [
            _security_violation(
                "weak-hsts",
                url,
                FindingSeverity.LOW,
                f"Strict-Transport-Security: max-age={max_age}.",
            )
        ]
    return []


def _cookie_violations(url: str, cookies: list[dict] | None) -> list[RawViolation]:
    """Cookie attribute rules, over cookies as Playwright reports them.

    Only attribute *names* and flags are read.  A cookie value never
    reaches this module, is never stored on a finding, and is never
    rendered into one — credentials keep their two exits.
    """
    found: list[RawViolation] = []
    https = url.lower().startswith("https://")
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name", "(unnamed)"))
        evidence = (f"cookie {name}",)
        if https and not cookie.get("secure"):
            found.append(
                _security_violation(
                    "cookie-missing-secure",
                    url,
                    FindingSeverity.HIGH,
                    f"Cookie {name} is set without Secure over HTTPS.",
                    evidence,
                )
            )
        if not cookie.get("httpOnly"):
            found.append(
                _security_violation(
                    "cookie-missing-httponly",
                    url,
                    FindingSeverity.MEDIUM,
                    f"Cookie {name} is readable from JavaScript.",
                    evidence,
                )
            )
        if not cookie.get("sameSite") or str(cookie.get("sameSite")).lower() == "none":
            found.append(
                _security_violation(
                    "cookie-missing-samesite",
                    url,
                    FindingSeverity.LOW,
                    f"Cookie {name} declares SameSite={cookie.get('sameSite') or 'unset'}.",
                    evidence,
                )
            )
    return found


def passive_security_violations(
    url: str,
    status: int | None = None,
    headers: dict[str, str] | None = None,
    cookies: list[dict] | None = None,
    body_sample: str | None = None,
) -> list[RawViolation]:
    """Every passive-security rule, judged against one response.

    The severity of each rule lives in this function and nowhere else — the
    same discipline axe's ``impact`` provides for accessibility.  A model is
    never asked, so two runs a week apart grade the same response the same.
    """
    if not isinstance(headers, dict) and headers is not None:
        raise CheckError("response headers are not a mapping")
    lower = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
    found: list[RawViolation] = []

    found.extend(_hsts_violations(url, lower.get("strict-transport-security")))

    if "content-security-policy" not in lower:
        found.append(
            _security_violation(
                "missing-csp", url, FindingSeverity.MEDIUM, "No Content-Security-Policy header."
            )
        )
    if lower.get("x-content-type-options", "").strip().lower() != "nosniff":
        found.append(
            _security_violation(
                "missing-x-content-type-options",
                url,
                FindingSeverity.LOW,
                f"X-Content-Type-Options: {lower.get('x-content-type-options') or 'absent'}.",
            )
        )
    if "referrer-policy" not in lower:
        found.append(
            _security_violation(
                "missing-referrer-policy", url, FindingSeverity.LOW, "No Referrer-Policy header."
            )
        )

    server = lower.get("server", "")
    if server and re.search(r"\d+\.\d+", server):
        found.append(
            _security_violation(
                "server-header-disclosure",
                url,
                FindingSeverity.LOW,
                f"Server: {server}",
            )
        )
    if "x-powered-by" in lower:
        found.append(
            _security_violation(
                "x-powered-by-disclosure",
                url,
                FindingSeverity.LOW,
                f"X-Powered-By: {lower['x-powered-by']}",
            )
        )

    if body_sample and (status is None or status >= 400):
        marker = next((m for m in _STACK_TRACE_MARKERS if m in body_sample), None)
        if marker is not None:
            found.append(
                _security_violation(
                    "error-response-leaks-a-stack-trace",
                    url,
                    FindingSeverity.HIGH,
                    f"A {status} response body contains {marker!r}.",
                )
            )

    found.extend(_cookie_violations(url, cookies))
    return found


# ── Run-scoped de-duplication ─────────────────────────────────────────


class RunViolations:
    """One run's violations, de-duplicated by ``(domain, rule, url)``.

    Owned by the task rather than by this module, because it is state and
    everything else here is a function.  That placement is also what makes
    its lifetime legible: a restarted run builds a fresh accumulator and
    re-walks the targets it has not finalized, so nothing here has to
    survive a worker dying.

    A rule met again at the same URL in a different page state is not a
    second finding — it is the same defect seen twice, and the state labels
    are kept on the one violation so the report can say where.
    """

    def __init__(self, max_violations: int | None = None):
        self._by_key: dict[tuple[str, str, str], RawViolation] = {}
        self._states: dict[tuple[str, str, str], list[str]] = {}
        self._max = max_violations
        self.dropped: int = 0

    def add(self, violation: RawViolation, state: str = "") -> bool:
        """Record a violation. Returns whether it opened a new entry."""
        key = violation.key
        if key in self._by_key:
            if state and state not in self._states[key]:
                self._states[key].append(state)
            return False
        if self._max is not None and len(self._by_key) >= self._max:
            self.dropped += 1
            return False
        self._by_key[key] = violation
        self._states[key] = [state] if state else []
        return True

    def extend(self, violations: list[RawViolation], state: str = "") -> None:
        for violation in violations:
            self.add(violation, state)

    def states_for(self, violation: RawViolation) -> list[str]:
        """Every page state this rule was seen in, in the order seen."""
        return list(self._states.get(violation.key, []))

    def all(self) -> list[RawViolation]:
        """Every distinct violation, in the order first seen."""
        return list(self._by_key.values())

    def __len__(self) -> int:
        return len(self._by_key)
