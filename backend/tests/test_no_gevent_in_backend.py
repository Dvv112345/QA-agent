"""The backend never imports Locust or gevent — only its load generator process does.

Importing Locust gevent-monkey-patches sockets and threads for the whole
interpreter. In the worker that would silently change psycopg2, RQ and
Playwright's sync API, and no functional test would notice, because every one
of them runs in an interpreter that has already imported everything else. So
this checks a **fresh** interpreter, where an import is the only way the
modules could appear.
"""

import ast
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent


def _modules_to_import() -> list[str]:
    """Every route, task and service module, plus the worker.

    Not ``backend.main``: it builds the app at import time, and that connects
    to PostgreSQL. Every router it mounts is in this list, so the list is a
    superset of what ``main`` pulls in. ``locust_profile`` is excluded
    because it *is* the load generator.
    """
    modules = ["backend.worker"]
    for package in ("routes", "tasks", "services"):
        for path in sorted((BACKEND / package).glob("*.py")):
            if path.stem in ("__init__", "locust_profile"):
                continue
            modules.append(f"backend.{package}.{path.stem}")
    return modules


def test_a_fresh_backend_interpreter_has_neither_locust_nor_gevent():
    code = (
        "import sys\n"
        + "".join(f"import {module}\n" for module in _modules_to_import())
        + "print(','.join(sorted(m for m in sys.modules "
        "if m.split('.')[0] in ('locust', 'gevent'))))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ""


def test_no_backend_module_imports_the_load_generator():
    """`locust_profile` is run as a file. Importing it would be importing Locust."""
    offenders = []
    for path in BACKEND.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [alias.name for alias in node.names]
            if any("locust" in name for name in names) and path.name != "locust_profile.py":
                offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == []
