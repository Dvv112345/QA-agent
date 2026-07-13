"""Requirement clarity analysis task, executed by the RQ worker.

One task function drives the whole lifecycle from row state: a row with a
``clarifying_question`` and a ``pending_answer`` gets the revision prompt,
anything else gets the initial clarity check.  Job args are the requirement
id only — everything else is read fresh from the database, which makes
every enqueue idempotent and reconciler-safe.

Must not import from ``backend.services.queue`` or ``backend.worker``
(circular-import rule).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlmodel import Session

from backend.config import MAX_AUTO_RETRIES, STORAGE_LOCATION, STORE_OFFLINE
from backend.database import new_session
from backend.models.database import Requirement, RequirementStatus, Sprint
from backend.services import llm
from backend.utils.crypto import decrypt_token
from backend.utils.github_utils import download_readme, parse_github_url

logger = logging.getLogger(__name__)

# Cap for the user-facing error summary stored on failed rows.
_ERROR_SUMMARY_MAX_CHARS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_readme(sprint: Sprint) -> str | None:
    """Best-effort README: stored copy → re-download → degrade to ``None``."""
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
        return asyncio.run(download_readme(owner, repo_name, token))
    except Exception as exc:
        logger.warning(
            "README download failed for sprint %d — analyzing without: %s", sprint.id, exc
        )
        return None


def _record_failure(session: Session, requirement_id: int, exc: Exception) -> None:
    """Count the failure and either re-queue the row or mark it failed."""
    session.rollback()
    requirement = session.get(Requirement, requirement_id)
    if requirement is None:
        return

    requirement.retry_count += 1
    if requirement.retry_count >= MAX_AUTO_RETRIES:
        requirement.status = RequirementStatus.FAILED
        requirement.error = str(exc)[:_ERROR_SUMMARY_MAX_CHARS]
    else:
        # Back to pending — the reconciler re-enqueues it.
        requirement.status = RequirementStatus.PENDING
    requirement.last_heartbeat = None
    requirement.updated_at = _now()
    session.add(requirement)
    session.commit()


def analyze_requirement_task(requirement_id: int) -> None:
    """Analyze one requirement's clarity (initial check or revision)."""
    with new_session() as session:
        requirement = session.get(Requirement, requirement_id)
        if requirement is None:
            logger.info("Requirement %d no longer exists — skipping", requirement_id)
            return
        if requirement.status not in (RequirementStatus.PENDING, RequirementStatus.ANALYZING):
            # Stale enqueue (e.g. user confirmed or edited meanwhile) — idempotency guard.
            logger.info(
                "Requirement %d is '%s' — skipping stale job", requirement_id, requirement.status
            )
            return

        requirement.status = RequirementStatus.ANALYZING
        requirement.last_heartbeat = _now()
        requirement.updated_at = _now()
        session.add(requirement)
        session.commit()

        try:
            sprint = requirement.sprint
            readme = _resolve_readme(sprint) if sprint else None
            file_tree = sprint.repo.file_tree if sprint and sprint.repo else None

            # Work-unit boundary before the (long) LLM call.
            requirement.last_heartbeat = _now()
            session.add(requirement)
            session.commit()

            if requirement.clarifying_question and requirement.pending_answer:
                result = llm.revise_requirement(
                    requirement.name,
                    requirement.description,
                    requirement.clarifying_question,
                    requirement.pending_answer,
                    readme,
                    file_tree,
                )
                requirement.description = result.rewritten_description
                requirement.revision_count += 1
                requirement.pending_answer = None
            else:
                result = llm.check_clarity(
                    requirement.name, requirement.description, readme, file_tree
                )

            if result.clear:
                requirement.status = RequirementStatus.READY
                requirement.clarifying_question = None
            else:
                requirement.status = RequirementStatus.NEEDS_CLARIFICATION
                requirement.clarifying_question = result.clarifying_question

            requirement.retry_count = 0
            requirement.last_heartbeat = None
            requirement.updated_at = _now()
            session.add(requirement)
            session.commit()
            logger.info("Requirement %d analyzed → %s", requirement_id, requirement.status)
        except Exception as exc:
            # Never re-raise: the DB retry counter, not RQ's failed registry,
            # is the recovery mechanism.
            logger.exception("Analysis failed for requirement %d", requirement_id)
            _record_failure(session, requirement_id, exc)
