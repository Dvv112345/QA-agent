"""Best-effort README resolution shared by routes and worker tasks."""

from __future__ import annotations

import logging
import os

from backend.config import STORAGE_LOCATION, STORE_OFFLINE
from backend.models.database import Sprint
from backend.utils.crypto import decrypt_token
from backend.utils.github_utils import download_readme, parse_github_url

logger = logging.getLogger(__name__)


async def resolve_readme(sprint: Sprint) -> str | None:
    """Best-effort README: stored copy → re-download → degrade to ``None``.

    Async so routes can await it directly; the worker task (no running
    loop) wraps it in ``asyncio.run``.
    """
    if STORE_OFFLINE:
        path = os.path.join(STORAGE_LOCATION, sprint.directory, "README.md")
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    return fh.read()
        except OSError as exc:
            logger.warning("Could not read stored README for sprint %d: %s", sprint.id, exc)

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
