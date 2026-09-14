"""Tests for backend/services/load_runner.py — against a real load generator.

Most tests here start the actual child process (``locust_profile.py``, Locust
in library mode) against a local HTTP stub. The stub counts what actually
arrived, which is the only way to test the claims that matter: exactly the
approved number of requests reach the host, a refused profile puts *nothing*
on it, and a stopped profile stops sending.

Durations stay at a few seconds (R7): the route's 50 s stress minimum is a
route rule, not a runner rule. A few tests swap in a tiny fake child to drive
the parent's crash and timeout paths without waiting for them.

The stub listens on 127.0.0.1, which the runner refuses by design — so every
test that wants traffic patches ``_is_private_host`` in *this* process, where
the refusal lives. The tests that assert the refusal do not.
"""

import datetime
import json
import os
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import backend.services.load_runner as load_runner
from backend.services.load_runner import (
    STOP_DURATION,
    STOP_ERROR_RATE,
    STOP_GENERATOR_EXITED,
    STOP_PARENT_EXITED,
    STOP_RUN_STOPPED,
    LoadResult,
    ceilings_for,
    refusal_for,
    resolve_body,
    run_profile,
    unknown_placeholders,
)
from backend.services.load_shapes import stages_for


class _Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str, str]] = []  # method, path, body
        self.in_flight = 0
        self.max_in_flight = 0
        self.last_headers: dict = {}

    def add(self, method, path, body, headers):
        with self.lock:
            self.requests.append((method, path, body))
            self.last_headers = headers
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            return len(self.requests)

    def done(self):
        with self.lock:
            self.in_flight -= 1

    @property
    def count(self) -> int:
        with self.lock:
            return len(self.requests)


def _start_stub(tls_context: ssl.SSLContext | None = None):
    recorder = _Recorder()
    behaviour = {"status": 200, "delay": 0.0, "redirect_to": None, "fail_after": None}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            number = recorder.add(self.command, self.path, body, dict(self.headers))
            try:
                if behaviour["delay"]:
                    time.sleep(behaviour["delay"])
                if behaviour["redirect_to"]:
                    self.send_response(302)
                    self.send_header("Location", behaviour["redirect_to"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                failing = behaviour["fail_after"] is not None and number > behaviour["fail_after"]
                payload = b"ok"
                self.send_response(500 if failing else behaviour["status"])
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                recorder.done()

        # BaseHTTPRequestHandler dispatches on these exact names.
        do_GET = _handle  # noqa: N815
        do_POST = _handle  # noqa: N815
        do_DELETE = _handle  # noqa: N815

        def log_message(self, *args):  # silence the stdlib access log
            pass

    class QuietServer(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass  # a generator killed mid-request leaves broken pipes behind

    server = QuietServer(("127.0.0.1", 0), Handler)
    scheme = "http"
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.recorder = recorder
    server.behaviour = behaviour
    server.url = f"{scheme}://127.0.0.1:{server.server_address[1]}/probe"
    return server


@pytest.fixture
def stub():
    """A local HTTP server that records every request it receives."""
    server = _start_stub()
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


@pytest.fixture
def kept_result_dir(tmp_path, monkeypatch):
    """Keep the generator's working directory, so a test can read what it wrote."""
    directory = tmp_path / "load-run"
    directory.mkdir()
    monkeypatch.setattr(load_runner.tempfile, "mkdtemp", lambda **kwargs: str(directory))
    monkeypatch.setattr(load_runner.shutil, "rmtree", lambda *args, **kwargs: None)
    return directory


def _fake_child(tmp_path, monkeypatch, source: str) -> None:
    """Swap the generator for a small script, to drive the parent's own paths."""
    script = tmp_path / "fake_child.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.setattr(load_runner, "_child_command", lambda: [sys.executable, str(script)])


# ── The cap ───────────────────────────────────────────────────────────


class TestRequestCap:
    def test_exactly_the_cap_arrives(self, stub, local_allowed):
        result = run_profile(url=stub.url, concurrency=2, duration_seconds=30, total_request_cap=25)

        assert stub.recorder.count == 25
        assert result.requests_sent == 25
        assert result.stopped_early is None  # the cap is the normal end
        assert result.refused is None

    def test_no_overshoot_when_every_user_races_the_last_slot(self, stub, local_allowed):
        """The case a "read, decide, send, increment" counter fails."""
        run_profile(url=stub.url, concurrency=10, duration_seconds=30, total_request_cap=11)

        assert stub.recorder.count == 11

    def test_the_duration_stop_fires_before_the_cap(self, stub, local_allowed):
        stub.behaviour["delay"] = 0.05

        result = run_profile(
            url=stub.url, concurrency=2, duration_seconds=2, total_request_cap=100_000
        )

        assert result.stopped_early == STOP_DURATION
        assert 0 < result.requests_sent < 100_000

    def test_the_total_is_clamped_to_config(self, stub, local_allowed, monkeypatch):
        monkeypatch.setattr(load_runner, "NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS", 3)

        result = run_profile(
            url=stub.url, concurrency=9999, duration_seconds=9999, total_request_cap=9999
        )

        assert stub.recorder.count == 3
        assert result.requests_sent == 3

    def test_the_command_is_the_generator_and_nothing_else(self):
        """No Locust CLI, and so no `--processes`: one process, one counter."""
        command = load_runner._child_command()

        assert command == [sys.executable, str(load_runner._LOCUSTFILE)]
        assert Path(command[1]).is_file()


# ── Refusals ──────────────────────────────────────────────────────────


@pytest.fixture
def no_popen(monkeypatch):
    calls: list = []

    def _popen(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a refused profile must never start the generator")

    monkeypatch.setattr(load_runner.subprocess, "Popen", _popen)
    return calls


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
    def test_private_address_space_is_refused_and_nothing_starts(self, url, no_popen):
        result = run_profile(url=url, total_request_cap=10)

        assert result.refused is not None
        assert result.requests_sent == 0
        assert no_popen == []

    def test_an_off_origin_url_is_refused_and_nothing_starts(self, stub, local_allowed, no_popen):
        result = run_profile(
            url=stub.url,
            allowed_origins={("https", "staging.example.com")},
            total_request_cap=10,
        )

        assert "not one of this run's confirmed origins" in result.refused
        assert no_popen == []
        assert stub.recorder.count == 0

    def test_a_non_http_url_is_refused(self, no_popen):
        assert run_profile(url="file:///etc/passwd").refused is not None
        assert no_popen == []

    def test_a_non_safe_method_needs_the_declaration(self, stub, local_allowed):
        refused = run_profile(url=stub.url, method="DELETE", total_request_cap=2)
        assert refused.refused is not None
        assert stub.recorder.count == 0

        allowed = run_profile(
            url=stub.url, method="DELETE", total_request_cap=2, environment_disposable=True
        )
        assert allowed.refused is None
        assert allowed.requests_sent == 2

    def test_ceilings_for_is_one_tier(self):
        safe = ceilings_for("GET", environment_disposable=False)
        unsafe = ceilings_for("POST", environment_disposable=True)

        assert safe == unsafe
        with pytest.raises(ValueError):
            ceilings_for("POST", environment_disposable=False)

    def test_a_soak_has_its_own_duration_ceiling(self, monkeypatch):
        monkeypatch.setattr(load_runner, "NONFUNCTIONAL_LOAD_SOAK_MAX_DURATION_SECONDS", 900)
        monkeypatch.setattr(load_runner, "NONFUNCTIONAL_LOAD_MAX_DURATION_SECONDS", 120)

        assert load_runner.duration_ceiling("soak") == 900
        assert load_runner.duration_ceiling("stress") == 120

    def test_refusal_for_answers_none_when_a_url_is_fine(self, monkeypatch):
        monkeypatch.setattr(load_runner, "_is_private_host", lambda host: False)
        assert refusal_for("https://staging.example.com/api") is None


# ── Bodies and credentials ────────────────────────────────────────────


class TestBodyPlaceholders:
    def test_a_resolved_value_reaches_the_host_and_nothing_else(
        self, stub, local_allowed, kept_result_dir, caplog
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
        written = [path for path in kept_result_dir.rglob("*") if path.is_file()]
        assert written  # the generator did write its result here
        for path in written:
            assert "s3cr3t-value" not in path.read_text(encoding="utf-8")

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
        assert result.responses == 10
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

    def test_requests_cut_off_at_the_stop_still_count_as_sent(self, stub, local_allowed):
        """R1: a killed in-flight request reached the host, so it was sent."""
        stub.behaviour["delay"] = 3.0

        result = run_profile(
            url=stub.url, concurrency=3, duration_seconds=1, total_request_cap=1000
        )

        assert result.requests_sent >= stub.recorder.count > 0
        assert result.responses < result.requests_sent

    def test_progress_carries_counters_and_never_samples(
        self, stub, local_allowed, kept_result_dir
    ):
        """R6: the per-second file must not grow with every request."""
        stub.behaviour["delay"] = 0.01

        run_profile(url=stub.url, concurrency=2, duration_seconds=3, total_request_cap=100_000)

        progress = json.loads((kept_result_dir / "progress.json").read_text(encoding="utf-8"))
        assert progress["requests_sent"] > 0
        assert "samples" not in progress
        assert "stages" not in progress
        assert not any(isinstance(value, list) for value in progress.values())


# ── Shapes ────────────────────────────────────────────────────────────


class TestShapes:
    def test_stress_steps_users_up_and_reports_every_step(self, stub, local_allowed):
        stub.behaviour["delay"] = 0.2

        result = run_profile(
            url=stub.url,
            shape="stress",
            concurrency=5,
            duration_seconds=5,
            total_request_cap=100_000,
        )

        assert result.shape == "stress"
        assert [row["users"] for row in result.stages] == [1, 2, 3, 4, 5]
        assert stub.recorder.max_in_flight >= 3
        assert len(result.derived["p95_by_users"]) == 5

    def test_a_stress_error_stop_reports_a_range_of_users(self, stub, local_allowed):
        stub.behaviour["fail_after"] = 20

        result = run_profile(
            url=stub.url,
            shape="stress",
            concurrency=5,
            duration_seconds=5,
            total_request_cap=100_000,
        )

        assert result.stopped_early == STOP_ERROR_RATE
        low, high = result.stopped_between_users
        assert low < high

    def test_a_spike_reports_its_three_phases(self, stub, local_allowed):
        stub.behaviour["delay"] = 0.02

        result = run_profile(
            url=stub.url,
            shape="spike",
            concurrency=10,
            duration_seconds=5,
            total_request_cap=100_000,
        )

        assert [row["name"] for row in result.stages] == ["baseline", "spike", "recovery"]
        assert set(result.derived) == {
            "baseline_p95_ms",
            "spike_p95_ms",
            "recovery_p95_ms",
            "recovery_p95_ratio",
        }


# ── Supervision: ticks, stops, crashes ────────────────────────────────


class TestTicks:
    def test_on_tick_is_called_while_the_generator_runs(self, stub, local_allowed):
        # Slow enough that the config's request ceiling cannot end it first.
        stub.behaviour["delay"] = 0.05
        calls: list[float] = []

        def _tick():
            calls.append(time.monotonic())
            return True

        result = run_profile(
            url=stub.url,
            duration_seconds=4,
            total_request_cap=100_000,
            on_tick=_tick,
            tick_interval_seconds=0.5,
        )

        assert result.stopped_early == STOP_DURATION
        assert len(calls) >= 3

    def test_on_tick_returning_false_kills_the_generator(self, stub, local_allowed):
        """D15: finishing a sprint mid-soak must stop the traffic."""
        stub.behaviour["delay"] = 0.01
        seen_traffic_at: list[int] = []

        def _tick():
            if stub.recorder.count and not seen_traffic_at:
                seen_traffic_at.append(0)
            if seen_traffic_at:
                seen_traffic_at[0] += 1
            # Let it run a couple of seconds after the first request, so a
            # progress file exists to report from.
            return not (seen_traffic_at and seen_traffic_at[0] > 5)

        started = time.monotonic()
        result = run_profile(
            url=stub.url,
            concurrency=2,
            duration_seconds=60,
            total_request_cap=1_000_000,
            on_tick=_tick,
            tick_interval_seconds=0.5,
        )
        elapsed = time.monotonic() - started

        count_after_kill = stub.recorder.count
        time.sleep(1.5)
        assert stub.recorder.count == count_after_kill  # nothing more reached the host
        assert elapsed < 30
        assert result.stopped_early == STOP_RUN_STOPPED
        assert result.requests_sent > 0

    def test_a_tick_that_raises_does_not_stop_the_profile(self, stub, local_allowed):
        stub.behaviour["delay"] = 0.05

        def _tick():
            raise RuntimeError("database blip")

        result = run_profile(
            url=stub.url,
            duration_seconds=2,
            total_request_cap=100_000,
            on_tick=_tick,
            tick_interval_seconds=0.2,
        )

        assert result.refused is None
        assert result.stopped_early == STOP_DURATION


class TestGeneratorFailures:
    def test_an_overrunning_generator_is_killed_and_reports_its_progress(
        self, tmp_path, monkeypatch, stub, local_allowed
    ):
        _fake_child(
            tmp_path,
            monkeypatch,
            "import json, os, time\n"
            "d = os.environ['QA_LOAD_RESULT_DIR']\n"
            "json.dump({'requests_sent': 7, 'responses': 6, 'status_counts': {'200': 6}},"
            " open(os.path.join(d, 'progress.json'), 'w'))\n"
            "time.sleep(60)\n",
        )

        started = time.monotonic()
        result = run_profile(
            url=stub.url, duration_seconds=1, total_request_cap=10, grace_seconds=1
        )

        assert time.monotonic() - started < 20
        assert result.stopped_early == STOP_GENERATOR_EXITED
        assert result.requests_sent == 7
        assert result.refused is None

    def test_a_generator_that_dies_without_a_result_is_a_redacted_refusal(
        self, tmp_path, monkeypatch, stub, local_allowed
    ):
        """Not a silent zero, which the task would stamp completed — and no credential."""
        _fake_child(
            tmp_path,
            monkeypatch,
            "import os, sys\n"
            "sys.stderr.write('Traceback: body was ' + os.environ['QA_LOAD_CONFIG'])\n"
            "sys.exit(1)\n",
        )

        result = run_profile(
            url=stub.url,
            method="POST",
            body='{"token": "$API_TOKEN"}',
            env_vars={"API_TOKEN": "s3cr3t-value"},
            cookies={"session": "browser-session-cookie-value"},
            total_request_cap=1,
            environment_disposable=True,
        )

        assert result.requests_sent == 0
        assert result.refused.startswith("Load generator could not start")
        assert "s3cr3t-value" not in result.refused
        assert "$API_TOKEN" in result.refused
        # The browser's cookies ride in the same config and are not env vars.
        assert "browser-session-cookie-value" not in result.refused
        assert "$COOKIE_session" in result.refused

    def test_a_job_timeout_propagates_and_the_generator_is_killed(
        self, tmp_path, monkeypatch, stub, local_allowed
    ):
        """RQ's timeout must reach RQ, and must not leave traffic running."""
        import psutil
        from rq.timeouts import JobTimeoutException

        pid_file = tmp_path / "child.pid"
        _fake_child(
            tmp_path,
            monkeypatch,
            "import os, time\n"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n",
        )

        def _tick():
            if pid_file.exists() and pid_file.read_text():
                raise JobTimeoutException("Task exceeded maximum timeout value")
            return True

        with pytest.raises(JobTimeoutException):
            run_profile(
                url=stub.url,
                duration_seconds=60,
                total_request_cap=10,
                on_tick=_tick,
                tick_interval_seconds=0.2,
            )

        assert not psutil.pid_exists(int(pid_file.read_text()))

    def test_a_missing_locust_is_a_refusal_not_a_raise(
        self, tmp_path, monkeypatch, stub, local_allowed
    ):
        _fake_child(tmp_path, monkeypatch, "import locust_that_is_not_installed\n")

        result = run_profile(url=stub.url, total_request_cap=1)

        assert isinstance(result, LoadResult)
        assert "could not start" in result.refused
        assert "ModuleNotFoundError" in result.refused

    def test_a_popen_failure_is_a_refusal(self, stub, local_allowed, monkeypatch):
        def _popen(*args, **kwargs):
            raise OSError("no such interpreter")

        monkeypatch.setattr(load_runner.subprocess, "Popen", _popen)

        result = run_profile(url=stub.url, total_request_cap=1)

        assert "no such interpreter" in result.refused


class TestWatchdog:
    def test_the_generator_exits_on_its_own_when_its_parent_is_gone(self, stub, tmp_path):
        """D15: a dead worker must not leave traffic running."""
        corpse = subprocess.Popen([sys.executable, "-c", "pass"])
        corpse.wait()
        config = load_runner._child_config(
            url=stub.url,
            method="GET",
            shape="load",
            body=None,
            headers=None,
            cookies=None,
            stages=stages_for("load", 1, 60),
            total_request_cap=1_000_000,
            request_timeout=5,
            error_rate_stop=0.5,
        )
        config["parent_pid"] = corpse.pid
        child = subprocess.Popen(
            load_runner._child_command(),
            env={
                **os.environ,
                "QA_LOAD_CONFIG": json.dumps(config),
                "QA_LOAD_RESULT_DIR": str(tmp_path),
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            child.wait(timeout=30)
        finally:
            if child.poll() is None:
                child.kill()

        result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
        assert result["stopped_early"] == STOP_PARENT_EXITED


class TestTls:
    def test_a_broken_ssl_cert_file_does_not_stop_the_generator_verifying(
        self, tmp_path, monkeypatch, local_allowed
    ):
        """R3: the generator verifies with certifi, not the inherited environment.

        A self-signed stub is *rejected* — which proves verification ran with a
        real bundle — rather than the generator dying on a missing file.
        """
        cert, key = _self_signed_pem(tmp_path)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server = _start_stub(context)
        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "does-not-exist.pem"))
        try:
            result = run_profile(url=server.url, total_request_cap=5, request_timeout=5)
        finally:
            server.shutdown()
            server.server_close()

        assert result.refused is None
        assert result.status_counts.get("error")


def _self_signed_pem(directory: Path) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "stub-cert.pem"
    key_path = directory / "stub-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)
