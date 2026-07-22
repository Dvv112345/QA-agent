"""Subprocess execution of generated test scripts.

Isolated from the worker task so the task's state machine stays testable
without a real Playwright/browser dependency in CI — task tests monkeypatch
``run_script`` entirely (mirrors how ``services.llm`` is monkeypatched
elsewhere). Runs inside the backend's own venv, so Playwright and its
browser binaries must be installed there (``playwright install chromium``,
a one-time worker-host prerequisite, not a CI dependency).

No sandboxing beyond the wall-clock timeout — accepted risk, per the
brainstorm's explicit decision.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass


@dataclass
class ScriptRunResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.exit_code == 0


def run_script(
    script: str, timeout: int, extra_env: dict[str, str] | None = None
) -> ScriptRunResult:
    """Write *script* to a temp file and run it with ``sys.executable``.

    ``extra_env`` is the cached env-vars dict for a test-script call — this
    function only ever runs test scripts (env-var extraction has no
    execution step of its own). Never raises.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        script_path = os.path.join(tmp_dir, "script.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        try:
            result = subprocess.run(
                [sys.executable, script_path],
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, **(extra_env or {})},
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            return ScriptRunResult(
                exit_code=-1,
                stdout=stdout,
                stderr=stderr + f"\n[timed out after {timeout}s]",
                timed_out=True,
            )

        return ScriptRunResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=False,
        )
