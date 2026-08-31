"""The ``read_file`` tool executor, shared by every stage that reads the repo.

Three LLM stages hand the model a ``read_file`` tool — test-script
generation, script diagnosis, CI/CD authoring, and the nonfunctional run
plan.  They all want the *same* executor, and for a while they had it as
three byte-identical private copies.  One home means a fix to the path
guard or the truncation rule reaches all of them.

The contract, which every caller depends on:

* **Path-validated** against the file tree the caller was given, so the
  model cannot read a path this sprint never captured.
* **Truncating** at ``TEST_EXECUTION_FILE_MAX_CHARS``.  The name is
  historical — it predates the other two callers — and is kept because
  ``.env.example`` is the source of truth for the variable list and
  renaming it would break existing environments.  It bounds all of them.
* **Never raises.**  Errors go back to the model as strings it can react
  to, because a tool that raises ends the loop while a tool that explains
  itself lets the model try another path.

``read_file`` calls ``asyncio.run`` internally, so it must be invoked from
a thread with no running event loop — an RQ task, or a route that reached
it through ``asyncio.to_thread``.  Calling it directly from an async route
raises.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from backend.config import TEST_EXECUTION_FILE_MAX_CHARS
from backend.utils import github_utils

_FILE_TRUNCATION_MARKER = "\n… (truncated)"


def build_read_file(
    file_tree: str, owner: str, repo: str, token: str | None
) -> Callable[[str], str]:
    """Build the LLM's ``read_file`` executor for one repository snapshot."""
    allowed_paths = set(file_tree.splitlines())

    def read_file(path: str) -> str:
        requested = (path or "").strip().lstrip("/")
        if requested not in allowed_paths:
            return f"ERROR: could not read '{requested}': path is not in the repository file tree."
        try:
            content = asyncio.run(github_utils.fetch_file(owner, repo, requested, token))
        except github_utils.GitHubError as exc:
            return f"ERROR: could not read '{requested}': {exc}"
        if content is None:
            return f"ERROR: could not read '{requested}': file not found."
        if len(content) > TEST_EXECUTION_FILE_MAX_CHARS:
            content = content[:TEST_EXECUTION_FILE_MAX_CHARS] + _FILE_TRUNCATION_MARKER
        return content

    return read_file
