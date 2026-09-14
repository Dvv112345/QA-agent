"""The one load generator: a load profile's config in, Locust users and a shape out.

**Never import this module.** ``load_runner.run_profile`` runs it as a separate
process (``python locust_profile.py``) and nothing else touches it, because
importing Locust gevent-monkey-patches ``socket``, ``ssl`` and ``threading``
for the whole interpreter. In the worker that would silently change psycopg2,
RQ and Playwright's sync API. ``test_no_gevent_in_backend`` pins the rule.

Locust runs as a **library** here (``Environment`` + a local runner), not via
``python -m locust``: the CLI imports a console-keypress listener that needs a
working pywin32 on Windows, and nothing else the CLI adds is wanted.

Inputs, from environment variables the parent sets::

    QA_LOAD_CONFIG      JSON: url, method, body (already resolved), headers,
                        cookies, shape, stages, total_request_cap,
                        request_timeout, error_rate_stop, parent_pid
    QA_LOAD_RESULT_DIR  where progress.json and result.json are written

The resolved body travels only in this process's environment — the same exit
``script_runner`` uses — and is written to neither file.

Lifecycle::

    spawning ─tick()─► running ─┬─ claim() refused (cap)       → drain, then quit
                                ├─ error rate > stop (≥ 5 resp) → drain, then quit
                                ├─ tick() → None                → stop now
                                └─ parent PID gone (watchdog)   → quit now

    stopped_early: cap → None · error rate → "error rate too high" ·
    tick → "duration reached" · watchdog → "worker process exited"

    "drain" = no new claims; quit when the last claimed request comes back,
    so every request counted as sent really went out.

    progress.json every second, counters only · result.json once, at the end
"""

from __future__ import annotations

import os
import sys

# Before any other import. Run as a script, this file's own directory is first
# on sys.path — and `backend/services/queue.py` there would shadow the stdlib
# `queue` that gevent and requests import.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [entry for entry in sys.path if os.path.abspath(entry or os.getcwd()) != _HERE]

import importlib.util  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import time  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402


def _load_sibling(name: str):
    """Import a stdlib-only sibling by path, now that its directory is off sys.path."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    # Registered before executing: dataclasses resolve string annotations
    # through sys.modules[cls.__module__].
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_shapes = _load_sibling("load_shapes")

import certifi  # noqa: E402
import gevent  # noqa: E402
import psutil  # noqa: E402
from locust import HttpUser, LoadTestShape, constant, task  # noqa: E402
from locust.env import Environment  # noqa: E402
from locust.exception import StopUser  # noqa: E402

PROGRESS_FILE = "progress.json"
RESULT_FILE = "result.json"

logging.getLogger("locust").setLevel(logging.WARNING)


class _State:
    """Everything the users share, and the only shared state.

    One process of cooperative greenlets: nothing between the check and the
    increment in ``claim`` can yield, so a claim is atomic without a lock.
    That is also why Locust's multi-process mode is never used — every
    process would get its own counter and the cap would multiply.
    """

    def __init__(self, cap: int, error_rate_stop: float):
        self.cap = cap
        self.error_rate_stop = error_rate_stop
        self.claimed = 0
        # Claimed and not yet finished. A stop waits for this to reach zero:
        # quitting kills users mid-request, and a request the user approved
        # and we claimed must actually go out rather than be counted and cut.
        self.in_flight = 0
        self.responses = 0
        self.errors = 0
        self.status_counts: dict[str, int] = {}
        self.samples: list[tuple[float, float, bool]] = []
        self.stop_reason: str | None = None
        self.error_stop_at_s: float | None = None
        self.stage: str | None = None
        self.started = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def claim(self) -> bool:
        """Claim one request slot. Returns whether it may be sent."""
        if self.stop_reason is not None:
            return False
        if self.claimed >= self.cap:
            self.stop_reason = load_shapes.STOP_CAP
            return False
        self.claimed += 1
        return True

    def record(self, latency_ms: float, status: int | None) -> bool:
        """Record one response. Returns whether the error-rate stop just fired."""
        elapsed = self.elapsed()
        is_error = status is None or status >= 500
        self.responses += 1
        key = str(status) if status is not None else "error"
        self.status_counts[key] = self.status_counts.get(key, 0) + 1
        if is_error:
            self.errors += 1
        self.samples.append((round(elapsed, 3), latency_ms, is_error))
        # Keep traffic off a host that is already failing. Judged over a
        # handful of responses at minimum, so one early refusal on a cold
        # connection cannot end a profile.
        if (
            self.stop_reason is None
            and self.responses >= 5
            and self.errors / self.responses > self.error_rate_stop
        ):
            self.stop_reason = load_shapes.STOP_ERROR_RATE
            self.error_stop_at_s = elapsed
            return True
        return False

    def error_rate(self) -> float:
        return round(self.errors / self.responses, 4) if self.responses else 0.0

    def progress(self, shape: str) -> dict:
        """Counters only: rewriting every sample each second would be quadratic."""
        return {
            "requests_sent": self.claimed,
            "responses": self.responses,
            "status_counts": dict(self.status_counts),
            "error_rate": self.error_rate(),
            "duration_ms": round(self.elapsed() * 1000, 2),
            "shape": shape,
            "stage": self.stage,
        }

    def final(self, shape: str, stages: list) -> dict:
        elapsed = self.elapsed()
        latencies = sorted(latency for _t, latency, _e in self.samples)
        derived = load_shapes.derived_figures(
            shape,
            stages,
            self.samples,
            error_stop_at_s=self.error_stop_at_s,
            elapsed_s=elapsed,
        )

        def _pct(fraction: float) -> float | None:
            return round(load_shapes.percentile(latencies, fraction), 2) if latencies else None

        return {
            # Claimed, not answered: a request cut off when the test stopped
            # still reached the host (R1).
            "requests_sent": self.claimed,
            "responses": self.responses,
            "p50_ms": _pct(0.50),
            "p95_ms": _pct(0.95),
            "p99_ms": _pct(0.99),
            "throughput_rps": (
                round(self.responses / elapsed, 2) if elapsed > 0 and self.responses else None
            ),
            "status_counts": dict(self.status_counts),
            "error_rate": self.error_rate(),
            # The cap is the normal end of a profile, not an early stop.
            "stopped_early": None if self.stop_reason == load_shapes.STOP_CAP else self.stop_reason,
            "duration_ms": round(elapsed * 1000, 2),
            "shape": shape,
            "stages": load_shapes.stage_figures(stages, self.samples),
            "derived": derived,
            "stopped_between_users": derived.get("stopped_between_users"),
        }


def _write_json(directory: str, name: str, payload: dict) -> None:
    """Atomic, so the parent never reads half a file after a kill."""
    path = os.path.join(directory, name)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(temporary, path)


def main() -> int:
    config = json.loads(os.environ["QA_LOAD_CONFIG"])
    result_dir = os.environ["QA_LOAD_RESULT_DIR"]

    shape = config["shape"]
    stages = [load_shapes.Stage(**stage) for stage in config["stages"]]
    state = _State(int(config["total_request_cap"]), float(config["error_rate_stop"]))
    url = config["url"]
    parts = urlsplit(url)
    method = config["method"]
    body = config.get("body")
    payload = body.encode("utf-8") if body is not None else None
    headers = dict(config.get("headers") or {})
    cookies = dict(config.get("cookies") or {})
    timeout = float(config["request_timeout"])
    parent_pid = int(config["parent_pid"])
    # Explicit, never the library default: this process inherits the
    # worker's environment unchanged, including a Windows SSL_CERT_FILE that
    # may point at a file that does not exist.
    ca_bundle = certifi.where()
    runner_box: dict = {}

    def request_quit() -> None:
        # From its own greenlet: `quit` kills every runner greenlet, and a
        # user greenlet calling it directly would be killing itself mid-call.
        if not runner_box.get("quitting"):
            runner_box["quitting"] = True
            gevent.spawn(runner_box["runner"].quit)

    class ProfileUser(HttpUser):
        host = f"{parts.scheme}://{parts.netloc}"
        wait_time = constant(0)

        def on_start(self):
            self.client.cookies.update(cookies)

        @task
        def send(self):
            if not state.claim():
                # Cap reached or a stop fired: this user is done. The run
                # ends once the requests already out have come back.
                if state.in_flight == 0:
                    request_quit()
                raise StopUser()
            state.in_flight += 1
            try:
                # Never follow a redirect: the origin lock was checked on the
                # URL we send, so a followed redirect would be a URL nobody
                # checked.
                self.client.request(
                    method,
                    url,
                    data=payload,
                    headers=headers,
                    allow_redirects=False,
                    timeout=timeout,
                    verify=ca_bundle,
                    name="profile",
                )
            finally:
                state.in_flight -= 1
                if state.stop_reason is not None and state.in_flight == 0:
                    request_quit()

    class StagesShape(LoadTestShape):
        def tick(self):
            elapsed = self.get_run_time()
            for stage in stages:
                if elapsed < stage.end_s:
                    state.stage = stage.name
                    return (stage.users, stage.spawn_rate)
            return None

    def on_request(response_time, response=None, **_kwargs) -> None:
        # No quit here: the error-rate stop only refuses further claims, and
        # the task's `finally` quits once the requests already out return.
        status = getattr(response, "status_code", None) or None
        state.record(round(response_time, 2), status)

    def ticker() -> None:
        while True:
            gevent.sleep(1)
            _write_json(result_dir, PROGRESS_FILE, state.progress(shape))
            if not psutil.pid_exists(parent_pid):
                # A dead worker must not leave traffic running on somebody's host.
                if state.stop_reason is None:
                    state.stop_reason = load_shapes.STOP_PARENT_EXITED
                request_quit()

    environment = Environment(user_classes=[ProfileUser], shape_class=StagesShape())
    environment.events.request.add_listener(on_request)
    runner = environment.create_local_runner()
    runner_box["runner"] = runner

    ticker_greenlet = gevent.spawn(ticker)
    state.started = time.monotonic()
    runner.start_shape()
    runner.shape_greenlet.join()
    # Blocking, and idempotent after a requested quit: every user is dead
    # before the figures are computed, so none lands after the result.
    runner.quit()
    ticker_greenlet.kill()

    if state.stop_reason is None:
        state.stop_reason = load_shapes.STOP_DURATION
    _write_json(result_dir, RESULT_FILE, state.final(shape, stages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
