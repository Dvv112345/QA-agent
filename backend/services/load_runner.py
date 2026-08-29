"""Apply one approved load profile and aggregate what came back.

The only module in this application that puts **sustained** traffic on a
host somebody else owns, which is why nearly all of it is refusals.

Three rules it enforces itself rather than trusting the route to have:

1. **Concurrency is a thread pool, never asyncio.** An event loop in the
   worker thread is exactly what Playwright's sync API refuses to coexist
   with, and a profile runs while a browser is open. ``asyncio.gather``
   here would work in every test and deadlock in production.
2. **The request cap is claimed, not checked.** ``concurrency`` workers
   that read a shared counter before dispatching all pass the same test at
   the same moment and overshoot by up to ``concurrency - 1``. A slot is
   claimed atomically and the request goes out only if the claimed index is
   under the cap.
3. **Never raises.** The task calls this directly, and a raise costs a
   retry — which then skips the profile anyway on ``requests_sent > 0``,
   so the run would fail for a reason nobody can act on. Every connection
   refused is a ``LoadResult`` with an error rate, not an exception.

``follow_redirects`` stays at httpx's ``False`` default, deliberately: the
origin lock is enforced on the URL we *send*, so a redirect we followed
would be a URL nobody checked.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

import httpx

from backend.config import (
    NONFUNCTIONAL_LOAD_ERROR_RATE_STOP,
    NONFUNCTIONAL_LOAD_MAX_CONCURRENCY,
    NONFUNCTIONAL_LOAD_MAX_DURATION_SECONDS,
    NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS,
    NONFUNCTIONAL_LOAD_REQUEST_TIMEOUT,
    NONFUNCTIONAL_LOAD_UNSAFE_MAX_CONCURRENCY,
    NONFUNCTIONAL_LOAD_UNSAFE_MAX_TOTAL_REQUESTS,
)
from backend.models.database import LoadMethod
from backend.utils.http_utils import SSL_CONTEXT

logger = logging.getLogger(__name__)

# `$NAME` / `${NAME}` placeholders in a request body, resolved here and
# nowhere else — see `resolve_body`.
_PLACEHOLDER = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")

# Reasons a profile stopped before its own limits.
STOP_DURATION = "duration reached"
STOP_CAP = "request cap reached"
STOP_ERROR_RATE = "error rate too high"


@dataclass
class LoadResult:
    """What one profile did, and what came back.

    Data only. Decision 11 keeps performance — single-request and load
    alike — out of findings entirely, so nothing here becomes a defect or
    a ticket. It describes the environment under load; it does not judge
    it, because a threshold we invented would be a verdict on somebody
    else's capacity planning.
    """

    requests_sent: int = 0
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    throughput_rps: float | None = None
    status_counts: dict[str, int] = field(default_factory=dict)
    error_rate: float = 0.0
    stopped_early: str | None = None
    duration_ms: float = 0.0
    # Why nothing was sent, when nothing was. Distinct from `stopped_early`,
    # which is about a profile that ran and then stopped.
    refused: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass(frozen=True)
class Ceilings:
    """The tier a profile runs under.

    Two tiers rather than one knob: safe methods only read, so they run
    against any confirmed origin; non-safe methods change data and run only
    on a run carrying the disposable-environment declaration, under a cap
    deliberately in the *tens*.
    """

    concurrency: int
    duration_seconds: int
    total_requests: int


def ceilings_for(method: str, *, environment_disposable: bool) -> Ceilings:
    """The tier this method runs under, or a refusal if it has none."""
    if LoadMethod.is_safe(method):
        return Ceilings(
            concurrency=NONFUNCTIONAL_LOAD_MAX_CONCURRENCY,
            duration_seconds=NONFUNCTIONAL_LOAD_MAX_DURATION_SECONDS,
            total_requests=NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS,
        )
    if not environment_disposable:
        raise ValueError(
            f"{method} changes data and this run does not carry the "
            "disposable-environment declaration."
        )
    return Ceilings(
        concurrency=NONFUNCTIONAL_LOAD_UNSAFE_MAX_CONCURRENCY,
        # A non-safe profile is bounded by its total, not its clock: the
        # duration ceiling stays the same number so a reader comparing two
        # profiles is not also comparing two time bases.
        duration_seconds=NONFUNCTIONAL_LOAD_MAX_DURATION_SECONDS,
        total_requests=NONFUNCTIONAL_LOAD_UNSAFE_MAX_TOTAL_REQUESTS,
    )


# ── Where a profile may point ─────────────────────────────────────────


def _is_private_host(host: str) -> bool:
    """Whether *host* names loopback, link-local, or private address space.

    The base URLs a run works from come out of ``env_vars_json``, which is
    free text, and the browser's origin lock accepts any ``http(s)`` netloc.
    Nothing else in this application would stop a profile aimed at
    ``http://localhost:8000`` — this application's own API — or at
    ``169.254.169.254``, the cloud metadata endpoint, at two thousand
    requests carrying the browser's session cookies.

    A single request to a host the user named is already permitted
    elsewhere (a tracker's ``base_url`` may name anything). Sustained
    server-side traffic is a different question, and this is the first
    feature that asks it.

    Resolution failures read as **private**: unknown is not proof of being
    safe to flood.
    """
    if not host:
        return True
    bare = host.strip("[]").lower()
    if bare in ("localhost", "localhost.localdomain") or bare.endswith(".localhost"):
        return True
    candidates: list[str] = []
    try:
        ipaddress.ip_address(bare)
        candidates.append(bare)
    except ValueError:
        try:
            infos = socket.getaddrinfo(bare, None)
        except OSError:
            logger.info("Load target %s does not resolve — refusing", host)
            return True
        candidates.extend(str(info[4][0]) for info in infos)
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            return True
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return True
    return False


def refusal_for(url: str, allowed: set[tuple[str, str]] | None = None) -> str | None:
    """Why this URL may not be loaded, or ``None`` if it may.

    Shared by the route and the executor on purpose: the route refuses at
    422 so the user learns before a run exists, and this module refuses
    again because a URL that reached it anyway must still not be flooded.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"{url!r} is not an http(s) URL."
    if allowed is not None and (parsed.scheme, parsed.netloc) not in allowed:
        return f"{parsed.scheme}://{parsed.netloc} is not one of this run's confirmed origins."
    if _is_private_host(parsed.hostname or ""):
        return (
            f"{parsed.hostname} is loopback, link-local, or private address space — "
            "load profiles may not be aimed there."
        )
    return None


# ── Body placeholders ─────────────────────────────────────────────────


def resolve_body(body: str | None, env_vars: dict[str, str] | None) -> str | None:
    """Substitute ``$NAME`` from the sprint's env vars, inside this module.

    The resolved text is never returned to a caller, never logged, and
    never stored: it goes straight into the request. That is what keeps a
    credential in a request body from becoming a third exit alongside
    ``fill_secret`` and ``create_issue``.
    """
    if not body:
        return body
    values = env_vars or {}

    def _replace(match: re.Match) -> str:
        return values.get(match.group(1), match.group(0))

    return _PLACEHOLDER.sub(_replace, body)


def unknown_placeholders(body: str | None, env_vars: dict[str, str] | None) -> list[str]:
    """Placeholder names a body uses that the sprint has no value for.

    The route's 422 material: a profile whose body still says ``$TOKEN``
    when it is sent tells the application under test nothing useful.
    """
    if not body:
        return []
    known = set(env_vars or {})
    return sorted({m.group(1) for m in _PLACEHOLDER.finditer(body)} - known)


# ── The run itself ────────────────────────────────────────────────────


class _Budget:
    """The shared stop condition, and the only piece of shared state.

    ``claim`` is one atomic operation on purpose. "Read the counter, decide,
    send, then increment" lets N workers all read ``sent < cap`` in the same
    instant and all proceed — an overshoot no single-threaded test can
    reproduce, and the one bug that would put more traffic on a host than
    the user approved.
    """

    def __init__(self, total_requests: int, deadline: float):
        self._lock = threading.Lock()
        self._claimed = 0
        self._total = total_requests
        self._deadline = deadline
        self.stopped: str | None = None
        self.completed = 0
        self.errors = 0
        self.latencies: list[float] = []
        self.status_counts: dict[str, int] = {}

    def claim(self) -> bool:
        """Claim one request slot. Returns whether it may be sent."""
        with self._lock:
            if self.stopped is not None:
                return False
            if self._claimed >= self._total:
                self.stopped = self.stopped or STOP_CAP
                return False
            if time.monotonic() >= self._deadline:
                self.stopped = self.stopped or STOP_DURATION
                return False
            self._claimed += 1
            return True

    def record(self, latency_ms: float, status: int | None, error_rate_stop: float) -> None:
        with self._lock:
            self.completed += 1
            self.latencies.append(latency_ms)
            key = str(status) if status is not None else "error"
            self.status_counts[key] = self.status_counts.get(key, 0) + 1
            if status is None or status >= 500:
                self.errors += 1
            # Keep traffic off a host that is already failing. Judged over a
            # handful of responses at minimum, so one early refusal on a
            # cold connection cannot end a profile.
            if self.completed >= 5 and self.errors / self.completed > error_rate_stop:
                self.stopped = self.stopped or STOP_ERROR_RATE


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile — no interpolation, no numpy."""
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, round(fraction * len(sorted_values) + 0.5) - 1))
    return sorted_values[index]


def _worker(
    *,
    budget: _Budget,
    url: str,
    method: str,
    body: str | None,
    headers: dict[str, str],
    cookies: dict[str, str],
    timeout: int,
    error_rate_stop: float,
) -> None:
    """One thread: claim a slot, send, record, repeat until the budget says stop."""
    # follow_redirects stays False — see the module docstring. A redirect we
    # followed would be a URL the origin lock never saw.
    with httpx.Client(
        verify=SSL_CONTEXT, timeout=timeout, follow_redirects=False, cookies=cookies
    ) as client:
        while budget.claim():
            started = time.monotonic()
            status: int | None = None
            try:
                response = client.request(method, url, content=body, headers=headers)
                status = response.status_code
            except Exception as exc:  # never raises — see the module docstring
                logger.debug("Load request to %s failed: %s", url, exc)
            budget.record((time.monotonic() - started) * 1000, status, error_rate_stop)


def run_profile(
    *,
    url: str,
    method: str = "GET",
    body: str | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    concurrency: int = 1,
    duration_seconds: int = 10,
    total_request_cap: int = 100,
    env_vars: dict[str, str] | None = None,
    environment_disposable: bool = False,
    allowed_origins: set[tuple[str, str]] | None = None,
    request_timeout: int = NONFUNCTIONAL_LOAD_REQUEST_TIMEOUT,
    error_rate_stop: float = NONFUNCTIONAL_LOAD_ERROR_RATE_STOP,
) -> LoadResult:
    """Apply one profile. Never raises; a refusal comes back as a result.

    ``cookies`` are always supplied by the caller — the browser's own, by
    decision, so a profile exercises the application as a logged-in user
    rather than measuring the latency of a redirect to a login page. The
    accepted consequence is that a non-safe profile performs up to
    ``NONFUNCTIONAL_LOAD_UNSAFE_MAX_TOTAL_REQUESTS`` authenticated writes,
    which is invisible in the data and therefore stated in the UI instead.
    """
    normalized = (method or "GET").upper()

    refusal = refusal_for(url, allowed_origins)
    if refusal is not None:
        return LoadResult(refused=refusal)
    try:
        ceilings = ceilings_for(normalized, environment_disposable=environment_disposable)
    except ValueError as exc:
        return LoadResult(refused=str(exc))

    # Clamp rather than refuse: the numbers came through a form, and a run
    # that quietly does less than asked is better than one that does more.
    workers = max(1, min(concurrency, ceilings.concurrency))
    seconds = max(1, min(duration_seconds, ceilings.duration_seconds))
    cap = max(1, min(total_request_cap, ceilings.total_requests))

    resolved_body = resolve_body(body, env_vars)
    budget = _Budget(total_requests=cap, deadline=time.monotonic() + seconds)
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in range(workers):
                pool.submit(
                    _worker,
                    budget=budget,
                    url=url,
                    method=normalized,
                    body=resolved_body,
                    headers=dict(headers or {}),
                    cookies=dict(cookies or {}),
                    timeout=request_timeout,
                    error_rate_stop=error_rate_stop,
                )
    except Exception as exc:  # pragma: no cover - defensive; the pool itself failing
        logger.exception("Load profile against %s could not run", url)
        return LoadResult(refused=f"Load profile could not run: {exc}")

    elapsed = time.monotonic() - started
    latencies = sorted(budget.latencies)
    sent = budget.completed
    return LoadResult(
        requests_sent=sent,
        p50_ms=round(_percentile(latencies, 0.50), 2) if latencies else None,
        p95_ms=round(_percentile(latencies, 0.95), 2) if latencies else None,
        p99_ms=round(_percentile(latencies, 0.99), 2) if latencies else None,
        throughput_rps=round(sent / elapsed, 2) if elapsed > 0 and sent else None,
        status_counts=dict(budget.status_counts),
        error_rate=round(budget.errors / sent, 4) if sent else 0.0,
        stopped_early=budget.stopped if budget.stopped != STOP_CAP else None,
        duration_ms=round(elapsed * 1000, 2),
    )
