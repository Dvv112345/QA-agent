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
from datetime import datetime, timezone

from sqlmodel import Session, select

from backend.config import MAX_AUTO_RETRIES
from backend.database import new_session
from backend.models.database import (
    SPRINT_FINISHED_ERROR,
    Requirement,
    RequirementStatus,
)
from backend.services import llm
from backend.utils.readme_utils import resolve_readme

logger = logging.getLogger(__name__)

# Cap for the user-facing error summary stored on failed rows.
_ERROR_SUMMARY_MAX_CHARS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        if requirement is None or requirement.archived:
            # Archived counts as gone: the user deleted this requirement
            # while the job was queued, and analyzing it would spend an LLM
            # call on a row nothing can display.
            logger.info("Requirement %d no longer exists — skipping", requirement_id)
            return
        if requirement.status not in (RequirementStatus.PENDING, RequirementStatus.ANALYZING):
            # Stale enqueue (e.g. user confirmed or edited meanwhile) — idempotency guard.
            logger.info(
                "Requirement %d is '%s' — skipping stale job", requirement_id, requirement.status
            )
            return

        sprint = requirement.sprint
        if sprint is None or not sprint.active:
            # Sprint finished (or vanished) after this job was enqueued —
            # mirror the finish-sprint sweep instead of analyzing.
            requirement.status = RequirementStatus.FAILED
            requirement.error = SPRINT_FINISHED_ERROR
            requirement.last_heartbeat = None
            requirement.pending_answer = None
            requirement.updated_at = _now()
            session.add(requirement)
            session.commit()
            logger.info("Requirement %d: sprint inactive — marked failed", requirement_id)
            return

        requirement.status = RequirementStatus.ANALYZING
        requirement.last_heartbeat = _now()
        requirement.updated_at = _now()
        session.add(requirement)
        session.commit()

        try:
            readme = asyncio.run(resolve_readme(sprint))
            file_tree = sprint.repo.file_tree if sprint.repo else None

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

            # The LLM call is the long wait — the row may have been failed
            # (sprint finished), deleted, or reset meanwhile. Re-read the
            # status and discard a stale result rather than overwrite it.
            # `archived` rides along: deleting a requirement mid-analysis
            # leaves its status untouched, so status alone would not notice.
            with session.no_autoflush:
                current = session.exec(
                    select(Requirement.status, Requirement.archived).where(
                        Requirement.id == requirement_id
                    )
                ).one_or_none()
            current_status = current[0] if current else None
            if current is None or current[1] or current_status != RequirementStatus.ANALYZING:
                logger.info(
                    "Requirement %d changed to '%s' mid-analysis — discarding result",
                    requirement_id,
                    current_status,
                )
                session.rollback()
                return

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
