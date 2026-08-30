"""Tests for backend/services/load_runner.py — against a local HTTP stub.

The stub counts what actually arrived, which is the only way to test the
claims that matter here: exactly the approved number of requests reach the
host, and a refused profile puts *nothing* on it.

The stub listens on 127.0.0.1, which the runner refuses by design — so
every test that wants traffic patches ``_is_private_host``. The tests that
assert the refusal do not, which is what keeps the seam honest.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import backend.services.load_runner as load_runner
from backend.services.load_runner import (
    STOP_DURATION,
    STOP_ERROR_RATE,
    LoadResult,
    ceilings_for,
    refusal_for,
    resolve_body,
    run_profile,
    unknown_placeholders,
)


class _Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str, str]] = []  # method, path, body

    def add(self, method, path, body):
        with self.lock:
            self.requests.append((method, path, body))

    @property
    def count(self) -> int:
        with self.lock:
            return len(self.requests)


@pytest.fixture
def stub():
    """A local HTTP server that records every request it receives."""
    recorder = _Recorder()
    behaviour = {"status": 200, "delay": 0.0, "redirect_to": None, "headers": {}}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            recorder.add(self.command, self.path, body)
            recorder.last_headers = dict(self.headers)
            if behaviour["delay"]:
                time.sleep(behaviour["delay"])
            if behaviour["redirect_to"]:
                self.send_response(302)
                self.send_header("Location", behaviour["redirect_to"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = b"ok"
            self.send_response(behaviour["status"])
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        # BaseHTTPRequestHandler dispatches on these exact names.
        do_GET = _handle  # noqa: N815
        do_POST = _handle  # noqa: N815
        do_DELETE = _handle  # noqa: N815

        def log_message(self, *args):  # silence the stdlib access log
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.recorder = recorder
    server.behaviour = behaviour
    server.url = f"http://127.0.0.1:{server.server_address[1]}/probe"
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def local_allowed(monkeypatch):
    """Let the runner reach the loopback stub.

    Patched rather than parameterised: a production switch that turns off
    the private-address refusal is a switch somebody eventually flips.
    """
    monkeypatch.setattr(load_runner, "_is_private_host", lambda host: False)


# ── The cap ───────────────────────────────────────────────────────────


class TestRequestCap:
    def test_exactly_the_cap_arrives(self, stub, local_allowed):
        result = run_profile(url=stub.url, concurrency=2, duration_seconds=30, total_request_cap=25)

        assert stub.recorder.count == 25
        assert result.requests_sent == 25
        assert result.stopped_early is None  # the cap is the normal end

    def test_no_overshoot_when_every_worker_races_the_last_slot(self, stub, local_allowed):
        """The case a "read, decide, send, increment" counter fails.

        Ten workers reading ``sent < cap`` in the same instant all proceed;
        a claimed slot cannot.
        """
        run_profile(url=stub.url, concurrency=10, duration_seconds=30, total_request_cap=11)

        assert stub.recorder.count == 11

    def test_the_duration_stop_fires_before_the_cap(self, stub, local_allowed):
        stub.behaviour["delay"] = 0.05

        result = run_profile(
            url=stub.url, concurrency=2, duration_seconds=1, total_request_cap=100_000
        )

        assert result.stopped_early == STOP_DURATION
        assert 0 < result.requests_sent < 100_000

    def test_concurrency_and_duration_are_clamped_to_config(self, stub, local_allowed, monkeypatch):
        monkeypatch.setattr(load_runner, "NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS", 3)

        result = run_profile(
            url=stub.url, concurrency=9999, duration_seconds=9999, total_request_cap=9999
        )

        assert stub.recorder.count == 3
        assert result.requests_sent == 3


# ── Refusals ──────────────────────────────────────────────────────────


class TestRefusals:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/api",
            "http://localhost:8000/api",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.1.2.3/internal",
            "http://192.168.0.5/admin",
        ],
    )
    def test_private_address_space_is_refused_and_nothing_is_sent(self, url, stub):
        result = run_profile(url=url, total_request_cap=10)

        assert result.refused is not None
        assert result.requests_sent == 0
        assert stub.recorder.count == 0

    def test_an_off_origin_url_is_refused_and_nothing_is_sent(self, stub, local_allowed):
        result = run_profile(
            url=stub.url,
            allowed_origins={("https", "staging.example.com")},
            total_request_cap=10,
        )

        assert "not one of this run's confirmed origins" in result.refused
        assert stub.recorder.count == 0

    def test_a_non_http_url_is_refused(self):
        assert run_profile(url="file:///etc/passwd").refused is not None

    def test_a_non_safe_method_needs_the_declaration(self, stub, local_allowed):
        refused = run_profile(url=stub.url, method="DELETE", total_request_cap=2)
        assert refused.refused is not None
        assert stub.recorder.count == 0

        allowed = run_profile(
            url=stub.url, method="DELETE", total_request_cap=2, environment_disposable=True
        )
        assert allowed.refused is None
        assert allowed.requests_sent == 2

    def test_the_unsafe_tier_binds_even_when_more_is_asked_for(self, stub, local_allowed):
        result = run_profile(
            url=stub.url,
            method="POST",
            concurrency=50,
            total_request_cap=5000,
            duration_seconds=30,
            environment_disposable=True,
        )

        assert stub.recorder.count == load_runner.NONFUNCTIONAL_LOAD_UNSAFE_MAX_TOTAL_REQUESTS
        assert result.requests_sent == load_runner.NONFUNCTIONAL_LOAD_UNSAFE_MAX_TOTAL_REQUESTS

    def test_ceilings_for_names_both_tiers(self):
        safe = ceilings_for("GET", environment_disposable=False)
        unsafe = ceilings_for("POST", environment_disposable=True)

        assert safe.total_requests > unsafe.total_requests
        assert safe.concurrency > unsafe.concurrency
        with pytest.raises(ValueError):
            ceilings_for("POST", environment_disposable=False)

    def test_refusal_for_answers_none_when_a_url_is_fine(self, monkeypatch):
        monkeypatch.setattr(load_runner, "_is_private_host", lambda host: False)
        assert refusal_for("https://staging.example.com/api") is None


# ── Bodies and credentials ────────────────────────────────────────────


class TestBodyPlaceholders:
    def test_a_placeholder_resolves_and_the_literal_stays_out_of_the_result(
        self, stub, local_allowed, caplog
    ):
        caplog.set_level("DEBUG")

        result = run_profile(
            url=stub.url,
            method="POST",
            body='{"token": "$API_TOKEN"}',
            env_vars={"API_TOKEN": "s3cr3t-value"},
            total_request_cap=1,
            environment_disposable=True,
        )

        assert stub.recorder.requests[0][2] == '{"token": "s3cr3t-value"}'
        assert "s3cr3t-value" not in result.to_json()
        assert "s3cr3t-value" not in caplog.text

    def test_an_unknown_placeholder_is_left_alone_and_reported(self):
        assert resolve_body("$A and $B", {"A": "1"}) == "1 and $B"
        assert unknown_placeholders("$A and $B", {"A": "1"}) == ["B"]

    def test_braced_placeholders_resolve_too(self):
        assert resolve_body("${A}/x", {"A": "1"}) == "1/x"

    def test_no_body_is_left_as_it_is(self):
        assert resolve_body(None, {"A": "1"}) is None
        assert unknown_placeholders(None, {}) == []


class TestCookies:
    def test_cookies_ride_on_every_request(self, stub, local_allowed):
        run_profile(url=stub.url, cookies={"session": "abc"}, total_request_cap=3)

        assert stub.recorder.count == 3
        assert "session=abc" in stub.recorder.last_headers.get("Cookie", "")


# ── Aggregation ───────────────────────────────────────────────────────


class TestAggregation:
    def test_percentiles_and_status_counts_come_from_real_timings(self, stub, local_allowed):
        stub.behaviour["delay"] = 0.01

        result = run_profile(url=stub.url, concurrency=2, total_request_cap=10)

        assert result.status_counts == {"200": 10}
        assert result.p50_ms >= 10
        assert result.p99_ms >= result.p50_ms
        assert result.throughput_rps > 0
        assert result.error_rate == 0.0

    def test_a_500_heavy_host_stops_early(self, stub, local_allowed):
        stub.behaviour["status"] = 500

        result = run_profile(url=stub.url, concurrency=1, total_request_cap=200)

        assert result.stopped_early == STOP_ERROR_RATE
        assert result.error_rate == 1.0
        assert result.requests_sent < 200

    def test_a_redirect_is_not_followed(self, stub, local_allowed):
        stub.behaviour["redirect_to"] = "https://elsewhere.example.com/"

        result = run_profile(url=stub.url, total_request_cap=2)

        assert result.status_counts == {"302": 2}

    def test_a_refused_connection_returns_a_result_rather_than_raising(self, local_allowed):
        # Port 1 on loopback: nothing listens, every connection refused.
        result = run_profile(
            url="http://127.0.0.1:1/", concurrency=2, total_request_cap=6, request_timeout=1
        )

        assert isinstance(result, LoadResult)
        assert result.error_rate == 1.0
        assert result.requests_sent > 0
        assert result.status_counts.get("error")

    def test_the_result_serializes(self, stub, local_allowed):
        result = run_profile(url=stub.url, total_request_cap=1)
        assert '"requests_sent": 1' in result.to_json()


class TestWorkersThatDieBeforeSending:
    """A profile that sent nothing because its threads died is not "sent nothing".

    `httpx.Client(verify=SSL_CONTEXT, ...)` raising is the documented
    Windows SSL_CERT_FILE footgun, and it happens *before* any slot is
    claimed — so the budget stays untouched and the result used to come
    back `requests_sent=0, refused=None`, which the task stamps COMPLETED
    with no error. Indistinguishable from a profile with nothing to do.
    """

    def test_every_worker_failing_is_a_refusal_not_a_silent_zero(
        self, stub, local_allowed, monkeypatch
    ):
        def _explode(*args, **kwargs):
            raise OSError("Could not find a suitable TLS CA certificate bundle")

        monkeypatch.setattr(load_runner.httpx, "Client", _explode)

        result = run_profile(url=stub.url, concurrency=2, total_request_cap=5)

        assert result.requests_sent == 0
        assert result.refused is not None
        assert "No request could be sent" in result.refused
        assert stub.recorder.count == 0

    def test_it_still_does_not_raise(self, stub, local_allowed, monkeypatch):
        def _explode(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(load_runner.httpx, "Client", _explode)

        assert isinstance(run_profile(url=stub.url, total_request_cap=1), LoadResult)


class TestPercentileRank:
    """Nearest rank, which is `ceil` — `round(x + 0.5)` is not.

    On an exact half Python rounds to even and goes *down*, so twenty
    samples put p95 on the maximum instead of the nineteenth value.
    """

    def test_p95_of_twenty_samples_is_the_nineteenth_not_the_maximum(self):
        values = [float(n) for n in range(1, 21)]

        assert load_runner._percentile(values, 0.95) == 19.0

    def test_p50_of_twenty_samples_is_the_tenth(self):
        values = [float(n) for n in range(1, 21)]

        assert load_runner._percentile(values, 0.50) == 10.0

    def test_an_empty_sample_is_zero(self):
        assert load_runner._percentile([], 0.95) == 0.0
