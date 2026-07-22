"""Tests for backend/services/script_runner.py — real subprocess mechanics.

No Playwright/browser dependency needed here (plain Python scripts suffice
to exercise the runner) — these run fast and need nothing external.
"""

from backend.services.script_runner import run_script


def test_passing_script_exits_zero():
    result = run_script("print('ok')", timeout=10)

    assert result.exit_code == 0
    assert result.passed is True
    assert "ok" in result.stdout
    assert result.timed_out is False


def test_failing_script_captures_stderr():
    script = "import sys\nsys.stderr.write('boom')\nsys.exit(1)"
    result = run_script(script, timeout=10)

    assert result.exit_code == 1
    assert result.passed is False
    assert "boom" in result.stderr


def test_timeout_marks_timed_out():
    script = "import time\ntime.sleep(5)"
    result = run_script(script, timeout=1)

    assert result.timed_out is True
    assert result.passed is False
    assert "timed out" in result.stderr


def test_extra_env_visible_to_subprocess():
    script = "import os\nprint(os.environ['MY_VAR'])"
    result = run_script(script, timeout=10, extra_env={"MY_VAR": "injected-value"})

    assert result.exit_code == 0
    assert "injected-value" in result.stdout
