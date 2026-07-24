"""Best-effort README/file-tree resolution shared by routes and worker tasks."""

from __future__ import annotations

import logging
import os

from backend.config import STORAGE_LOCATION, STORE_OFFLINE
from backend.models.database import Sprint
from backend.services.storage import StorageService
from backend.utils.crypto import decrypt_token
from backend.utils.github_utils import (
    download_readme,
    fetch_file_tree,
    fetch_repo_metadata,
    parse_github_url,
)

logger = logging.getLogger(__name__)


async def _download(sprint: Sprint) -> str | None:
    """Download the README directly from GitHub. Never raises — logs a
    warning and returns ``None`` on any failure."""
    repo = sprint.repo
    if repo is None:
        return None
    try:
        owner, repo_name = parse_github_url(repo.github_link)
        token = decrypt_token(repo.github_token) if repo.github_token else None
        return await download_readme(owner, repo_name, token)
    except Exception as exc:
        logger.warning(
            "README download failed for sprint %d — analyzing without: %s", sprint.id, exc
        )
        return None


async def resolve_readme(sprint: Sprint, *, force_refresh: bool = False) -> str | None:
    """Best-effort README: stored copy → re-download → degrade to ``None``.

    ``force_refresh=True`` re-downloads from GitHub first and refreshes the
    stored copy on success; on failure it falls back to the stored/cached
    behavior below rather than losing README context entirely. Callers
    should only pass ``force_refresh=True`` for sprints whose README was
    not user-uploaded (a user-supplied README is authoritative).

    Async so routes can await it directly; the worker task (no running
    loop) wraps it in ``asyncio.run``.
    """
    if force_refresh:
        fresh = await _download(sprint)
        if fresh is not None:
            if STORE_OFFLINE:
                StorageService().store_readme(fresh.encode("utf-8"), sprint.directory)
            return fresh
        # GitHub unreachable / no README — fall through to cached copy.

    if STORE_OFFLINE:
        path = os.path.join(STORAGE_LOCATION, sprint.directory, "README.md")
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    return fh.read()
        except OSError as exc:
            logger.warning("Could not read stored README for sprint %d: %s", sprint.id, exc)

    return await _download(sprint)


async def refresh_file_tree(sprint: Sprint) -> str | None:
    """Best-effort refresh of the repo's file tree from GitHub.

    Mutates and returns ``sprint.repo.file_tree`` on success (caller
    commits); on any failure, logs a warning and returns the existing
    value unchanged — mirrors the refresh done at sprint creation
    (``routes/sprints.py``).
    """
    repo = sprint.repo
    if repo is None:
        return None
    try:
        owner, repo_name = parse_github_url(repo.github_link)
        token = decrypt_token(repo.github_token) if repo.github_token else None
        metadata = await fetch_repo_metadata(owner, repo_name, token)
        repo.file_tree = await fetch_file_tree(owner, repo_name, metadata["default_branch"], token)
    except Exception as exc:
        logger.warning(
            "File tree refresh failed for sprint %d — using existing copy: %s", sprint.id, exc
        )
    return repo.file_tree
