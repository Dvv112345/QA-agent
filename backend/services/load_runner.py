"""Apply one approved load profile and aggregate what came back.

The only module in this application that puts **sustained** traffic on a
host somebody else owns, which is why nearly all of it is refusals.

Rules it enforces itself rather than trusting the route to have:

1. **The traffic comes from another process, never from this one.** The
   generator is Locust, and importing Locust gevent-monkey-patches the whole
   interpreter — in the worker, psycopg2, RQ and Playwright's sync API would
   all change underneath us. ``locust_profile.py`` is run with
   ``sys.executable`` and never imported; a fresh-interpreter test pins it.
2. **The request cap is claimed, not checked** — inside that process, where
   a claim cannot yield, and never with Locust's multi-process mode, which
   would hand every process its own counter.
3. **Never raises.** The task calls this directly, and a raise costs a
   retry that could not tell what was sent. Every connection refused is a
   ``LoadResult`` with an error rate, not an exception.
4. **The parent keeps the child honest.** It polls, calls ``on_tick``
   (heartbeat, and "should this keep going?"), and kills the child when told
   to stop or when ``duration + grace`` passes. The child watches this
   process in turn and exits if it disappears.

Redirects are never followed: the origin lock is enforced on the URL we
*send*, so a redirect we followed would be a URL nobody checked.

The poll loop::

    Popen(child) ─► communicate(timeout=1) ─ exited ─► result.json
          ▲                │ still running             └─ else progress.json
          │                ▼                            └─ else refused (redacted stderr)
          │    every tick_interval: on_tick()
          │        False ─► kill ─► progress.json, "run stopped"
          └─── now > duration + grace ─► kill ─► progress.json, "load generator exited"
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse

from backend.config import (
    HEARTBEAT_STALE_SECONDS,
    NONFUNCTIONAL_LOAD_ERROR_RATE_STOP,
    NONFUNCTIONAL_LOAD_MAX_CONCURRENCY,
    NONFUNCTIONAL_LOAD_MAX_DURATION_SECONDS,
    NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS,
    NONFUNCTIONAL_LOAD_PROCESS_GRACE_SECONDS,
    NONFUNCTIONAL_LOAD_REQUEST_TIMEOUT,
    NONFUNCTIONAL_LOAD_SOAK_MAX_DURATION_SECONDS,
)
from backend.models.database import LoadMethod, LoadShape
from backend.services import load_shapes
from backend.services.load_shapes import (  # noqa: F401 — re-exported for callers
    STOP_CAP,
    STOP_DURATION,
    STOP_ERROR_RATE,
    STOP_GENERATOR_EXITED,
    STOP_PARENT_EXITED,
    STOP_RUN_STOPPED,
)
from backend.utils.environment_utils import redact, redactable_items, url_values

logger = logging.getLogger(__name__)

# `$NAME` / `${NAME}` placeholders in a request body, resolved here and
# nowhere else — see `resolve_body`.
_PLACEHOLDER = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")

# The load generator. Run, never imported — see rule 1 above.
_LOCUSTFILE = Path(__file__).with_name("locust_profile.py")
_PROGRESS_FILE = "progress.json"
_RESULT_FILE = "result.json"
# How much of a dead child's stderr reaches `profile.error`, after redaction.
_STDERR_TAIL_CHARS = 2000
# How long to wait for a killed child to be reaped.
_REAP_TIMEOUT_SECONDS = 10


@dataclass
class LoadResult:
    """What one profile did, and what came back.

    Data only. Decision 11 keeps performance — single-request and load
    alike — out of findings entirely, so nothing here becomes a defect or
    a ticket. It describes the environment under load; it does not judge
    it, because a threshold we invented would be a verdict on somebody
    else's capacity planning.
    """

    # Requests the generator *claimed*, i.e. started. Not responses: a
    # request cut off when the test stopped still reached the host, and this
    # is the number that says how much traffic went out (R1).
    requests_sent: int = 0
    responses: int = 0
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    throughput_rps: float | None = None
    status_counts: dict[str, int] = field(default_factory=dict)
    error_rate: float = 0.0
    stopped_early: str | None = None
    duration_ms: float = 0.0
    shape: str = LoadShape.LOAD.value
    # Each stage's definition and figures, written with the result so a later
    # formula change cannot re-describe this run (D6).
    stages: list[dict] = field(default_factory=list)
    derived: dict = field(default_factory=dict)
    stopped_between_users: list[int] | None = None
    # Why nothing was sent, when nothing was. Distinct from `stopped_early`,
    # which is about a profile that ran and then stopped.
    refused: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_child(cls, payload: dict) -> LoadResult:
        """Build from the generator's JSON, ignoring keys this side does not know."""
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in payload.items() if key in known})


@dataclass(frozen=True)
class Ceilings:
    """The ceilings a profile runs under.

    One tier for every method. What separates a non-safe method from a safe
    one is permission, not size: it needs the run's disposable-environment
    declaration, and once it has it, it runs under these same numbers.
    """

    concurrency: int
    duration_seconds: int
    total_requests: int


def ceilings_for(method: str, *, environment_disposable: bool) -> Ceilings:
    """The ceilings this method runs under, or a refusal if it may not run."""
    if not LoadMethod.is_safe(method) and not environment_disposable:
        raise ValueError(
            f"{method} changes data and this run does not carry the "
            "disposable-environment declaration."
        )
    return Ceilings(
        concurrency=NONFUNCTIONAL_LOAD_MAX_CONCURRENCY,
        duration_seconds=NONFUNCTIONAL_LOAD_MAX_DURATION_SECONDS,
        total_requests=NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS,
    )


def duration_ceiling(shape: str) -> int:
    """The longest a profile of this shape may run: a soak has its own ceiling."""
    if shape == LoadShape.SOAK:
        return NONFUNCTIONAL_LOAD_SOAK_MAX_DURATION_SECONDS
    return NONFUNCTIONAL_LOAD_MAX_DURATION_SECONDS


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


def allowed_origins_for(base_urls: list[str]) -> set[tuple[str, str]]:
    """The origins a run's profiles may be aimed at.

    Deliberately the browser's own rule, imported rather than re-derived:
    a load profile and a typed navigation are the same question about the
    same run — may this reach that host — and two answers to it is the shape
    of bug that only shows up in production.
    """
    from backend.services.browser_session import allowed_origins

    return allowed_origins(base_urls)


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


def preflight(
    url: str,
    method: str,
    allowed: set[tuple[str, str]] | None,
    *,
    environment_disposable: bool,
) -> str | None:
    """Every refusal that puts nothing on the wire, or ``None`` if it may run.

    Separate from ``run_profile`` so the task can ask it *before* stamping
    ``launched_at``: a profile refused here never sent anything, so it must
    not carry the stamp that says it might have. ``run_profile`` asks again
    rather than trusting that the caller did.
    """
    refusal = refusal_for(url, allowed)
    if refusal is not None:
        return refusal
    try:
        ceilings_for((method or "GET").upper(), environment_disposable=environment_disposable)
    except ValueError as exc:
        return str(exc)
    return None


# ── Body placeholders ─────────────────────────────────────────────────


def resolve_body(body: str | None, env_vars: dict[str, str] | None) -> str | None:
    """Substitute ``$NAME`` from the sprint's env vars, inside this module.

    The resolved text is never returned to a caller, never logged, and
    never stored: it goes straight into the generator's environment. That is
    what keeps a credential in a request body from becoming a third exit
    alongside ``fill_secret`` and ``create_issue``.
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


def _child_command() -> list[str]:
    """The generator's command line. No flags: Locust runs as a library in there."""
    return [sys.executable, str(_LOCUSTFILE)]


def _child_config(
    *,
    url: str,
    method: str,
    shape: str,
    body: str | None,
    headers: dict[str, str] | None,
    cookies: dict[str, str] | None,
    stages: list[load_shapes.Stage],
    total_request_cap: int,
    request_timeout: int,
    error_rate_stop: float,
) -> dict:
    return {
        "url": url,
        "method": method,
        "shape": shape,
        "body": body,
        "headers": dict(headers or {}),
        "cookies": dict(cookies or {}),
        "stages": [stage.to_dict() for stage in stages],
        "total_request_cap": total_request_cap,
        "request_timeout": request_timeout,
        "error_rate_stop": error_rate_stop,
        "parent_pid": os.getpid(),
    }


def run_profile(
    *,
    url: str,
    method: str = "GET",
    shape: str = LoadShape.LOAD.value,
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
    on_tick: Callable[[], bool] | None = None,
    tick_interval_seconds: float | None = None,
    grace_seconds: float | None = None,
) -> LoadResult:
    """Apply one profile. Never raises; a refusal comes back as a result.

    ``cookies`` are always supplied by the caller — the browser's own, by
    decision, so a profile exercises the application as a logged-in user
    rather than measuring the latency of a redirect to a login page. The
    accepted consequence is that a non-safe profile performs up to its
    request cap in authenticated writes, which is invisible in the data and
    therefore stated in the UI instead.

    ``on_tick`` is called about every ``tick_interval_seconds`` (a third of
    the heartbeat threshold by default) while the generator runs. It is the
    caller's heartbeat, and its return value is the stop switch: ``False``
    kills the generator. A tick that raises counts as "keep going" — a
    database blip must not end a profile, and the wall clock still bounds it.
    """
    normalized = (method or "GET").upper()

    refusal = preflight(
        url, normalized, allowed_origins, environment_disposable=environment_disposable
    )
    if refusal is not None:
        return LoadResult(refused=refusal)
    ceilings = ceilings_for(normalized, environment_disposable=environment_disposable)

    # Clamp rather than refuse: the numbers came through a form, and a run
    # that quietly does less than asked is better than one that does more.
    users = max(1, min(concurrency, ceilings.concurrency))
    seconds = max(1, min(duration_seconds, duration_ceiling(shape)))
    cap = max(1, min(total_request_cap, ceilings.total_requests))

    config = _child_config(
        url=url,
        method=normalized,
        shape=shape,
        body=resolve_body(body, env_vars),
        headers=headers,
        cookies=cookies,
        stages=load_shapes.stages_for(shape, users, seconds),
        total_request_cap=cap,
        request_timeout=request_timeout,
        error_rate_stop=error_rate_stop,
    )
    tick_interval = (
        tick_interval_seconds
        if tick_interval_seconds is not None
        else max(1.0, HEARTBEAT_STALE_SECONDS / 3)
    )
    grace = grace_seconds if grace_seconds is not None else NONFUNCTIONAL_LOAD_PROCESS_GRACE_SECONDS
    try:
        return _run_child(config, env_vars, seconds + grace, on_tick, tick_interval)
    except Exception as exc:  # never raises — see the module docstring
        logger.exception("Load profile against %s could not run", url)
        return LoadResult(refused=f"Load generator could not run: {exc}")


def _run_child(
    config: dict,
    env_vars: dict[str, str] | None,
    wall_seconds: float,
    on_tick: Callable[[], bool] | None,
    tick_interval: float,
) -> LoadResult:
    result_dir = tempfile.mkdtemp(prefix="qa-load-")
    try:
        # The resolved body rides in the child's environment and nowhere
        # else: not argv (visible to every process listing), not a file.
        child_env = {
            **os.environ,
            "QA_LOAD_CONFIG": json.dumps(config),
            "QA_LOAD_RESULT_DIR": result_dir,
        }
        try:
            process = subprocess.Popen(
                _child_command(),
                env=child_env,
                cwd=result_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            return LoadResult(refused=f"Load generator could not start: {exc}")
        outcome, stderr = _supervise(process, wall_seconds, on_tick, tick_interval)
        return _read_result(result_dir, outcome, stderr, env_vars)
    finally:
        shutil.rmtree(result_dir, ignore_errors=True)


def _supervise(
    process: subprocess.Popen,
    wall_seconds: float,
    on_tick: Callable[[], bool] | None,
    tick_interval: float,
) -> tuple[str, bytes]:
    """Wait for the child, ticking and enforcing the wall clock.

    Returns ``("exited" | "stopped" | "timed_out", stderr)``. ``communicate``
    keeps draining stderr between timeouts, so a chatty child cannot fill the
    pipe and block itself.
    """
    started = time.monotonic()
    last_tick = started
    while True:
        try:
            _stdout, stderr = process.communicate(timeout=1)
            return "exited", stderr or b""
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if on_tick is not None and now - last_tick >= tick_interval:
            last_tick = now
            if not _keep_going(on_tick):
                return "stopped", _kill(process)
        if now - started >= wall_seconds:
            logger.warning("Load generator overran %.0f s — killing it", wall_seconds)
            return "timed_out", _kill(process)


def _keep_going(on_tick: Callable[[], bool]) -> bool:
    try:
        return on_tick() is not False
    except Exception:
        logger.exception("Load profile tick raised — continuing")
        return True


def _kill(process: subprocess.Popen) -> bytes:
    """Kill and reap, so no generator outlives its profile (Windows included)."""
    process.kill()
    try:
        _stdout, stderr = process.communicate(timeout=_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.error("Load generator pid %d did not exit after kill", process.pid)
        return b""
    return stderr or b""


def _load_json(directory: str, name: str) -> dict | None:
    try:
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_result(
    result_dir: str, outcome: str, stderr: bytes, env_vars: dict[str, str] | None
) -> LoadResult:
    """The final result if there is one, else the last progress, else why not."""
    final = _load_json(result_dir, _RESULT_FILE)
    if final is not None:
        return LoadResult.from_child(final)

    progress = _load_json(result_dir, _PROGRESS_FILE)
    reason = STOP_RUN_STOPPED if outcome == "stopped" else STOP_GENERATOR_EXITED
    if progress is not None:
        partial = LoadResult.from_child(progress)
        partial.stopped_early = reason
        return partial
    if outcome == "stopped":
        # Killed before its first progress write: nothing is known, and
        # `launched_at` is what keeps it from being sent again.
        return LoadResult(stopped_early=STOP_RUN_STOPPED)

    tail = _stderr_tail(stderr, env_vars)
    logger.error("Load generator produced no result: %s", tail)
    return LoadResult(
        refused=f"Load generator could not start: {tail}"
        if tail
        else "Load generator exited without a result."
    )


def _stderr_tail(stderr: bytes, env_vars: dict[str, str] | None) -> str:
    """The end of the child's stderr with every credential replaced by its ``$NAME``.

    A traceback can quote the request body, which holds resolved values.
    URLs are kept: a human reads `profile.error`, and a failure about a page
    has to be allowed to name the page.
    """
    text = stderr.decode("utf-8", errors="replace")[-_STDERR_TAIL_CHARS:].strip()
    return redact(text, redactable_items(env_vars, keep=url_values(env_vars)))
