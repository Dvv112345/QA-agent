"""Test-plan generation task, executed by the RQ worker.

One task function drives the whole lifecycle from row state: a row with
``pending_feedback`` set gets the revision prompt, anything else gets the
initial generation.  Job args are the test-plan id only — everything else
is read fresh from the database, which makes every enqueue idempotent and
reconciler-safe.

Must not import from ``backend.services.queue`` or ``backend.worker``
(circular-import rule).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from backend.config import MAX_AUTO_RETRIES
from backend.database import new_session
from backend.models.database import (
    SPRINT_FINISHED_ERROR,
    TestCase,
    TestPlan,
    TestPlanStatus,
)
from backend.services import llm
from backend.utils.readme_utils import resolve_readme

logger = logging.getLogger(__name__)

# Cap for the user-facing error summary stored on failed rows.
_ERROR_SUMMARY_MAX_CHARS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _record_failure(session: Session, test_plan_id: int, exc: Exception) -> None:
    """Count the failure and either re-queue the row or mark it failed.

    ``pending_feedback`` is deliberately preserved so a Restart resumes an
    interrupted feedback revision.
    """
    session.rollback()
    plan = session.get(TestPlan, test_plan_id)
    if plan is None:
        return

    plan.retry_count += 1
    if plan.retry_count >= MAX_AUTO_RETRIES:
        plan.status = TestPlanStatus.FAILED
        plan.error = str(exc)[:_ERROR_SUMMARY_MAX_CHARS]
    else:
        # Back to pending — the reconciler re-enqueues it.
        plan.status = TestPlanStatus.PENDING
    plan.last_heartbeat = None
    plan.updated_at = _now()
    session.add(plan)
    session.commit()


def _serialize_plan(plan: TestPlan) -> str:
    """Current plan as JSON in the ``TestPlanResult`` shape (steps as lists)."""
    return json.dumps(
        {
            "complexity": plan.complexity,
            "summary": plan.summary,
            "cases": [
                {
                    "title": case.title,
                    "preconditions": case.preconditions,
                    "steps": case.steps.split("\n"),
                    "expected_result": case.expected_result,
                    "case_type": case.case_type,
                    "priority": case.priority,
                }
                for case in plan.cases
            ],
        }
    )


def generate_test_plan_task(test_plan_id: int) -> None:
    """Generate or revise one requirement's test plan."""
    with new_session() as session:
        plan = session.get(TestPlan, test_plan_id)
        if plan is None:
            logger.info("Test plan %d no longer exists — skipping", test_plan_id)
            return
        if plan.status not in (TestPlanStatus.PENDING, TestPlanStatus.GENERATING):
            # Stale enqueue (e.g. user approved or edited meanwhile) — idempotency guard.
            logger.info("Test plan %d is '%s' — skipping stale job", test_plan_id, plan.status)
            return

        requirement = plan.requirement
        sprint = requirement.sprint if requirement is not None else None
        if sprint is None or not sprint.active:
            # Sprint finished (or vanished) after this job was enqueued —
            # mirror the finish-sprint sweep instead of generating.
            plan.status = TestPlanStatus.FAILED
            plan.error = SPRINT_FINISHED_ERROR
            plan.last_heartbeat = None
            plan.pending_feedback = None
            plan.updated_at = _now()
            session.add(plan)
            session.commit()
            logger.info("Test plan %d: sprint inactive — marked failed", test_plan_id)
            return

        plan.status = TestPlanStatus.GENERATING
        plan.last_heartbeat = _now()
        plan.updated_at = _now()
        session.add(plan)
        session.commit()

        try:
            readme = asyncio.run(resolve_readme(sprint))
            file_tree = sprint.repo.file_tree if sprint.repo else None
            test_env_content = sprint.test_environment.content if sprint.test_environment else None
            sibling_names = [
                r.name
                for r in sorted(
                    (r for r in sprint.requirements if r.id != requirement.id),
                    key=lambda r: (r.created_at, r.id),
                )
            ]

            # Heartbeat immediately before the LLM call. Generation is a
            # single completion now, so the gap from here to the write below
            # is one request bounded by OPENAI_TIMEOUT — well inside
            # HEARTBEAT_STALE_SECONDS. That is why no per-round callback is
            # passed any more: there are no rounds, and a second stamp
            # microseconds after this one would report nothing new.
            plan.last_heartbeat = _now()
            session.add(plan)
            session.commit()

            is_revision = bool(plan.pending_feedback)
            if is_revision:
                result = llm.revise_test_plan(
                    name=requirement.name,
                    description=requirement.description,
                    sibling_names=sibling_names,
                    test_env_content=test_env_content,
                    readme=readme,
                    file_tree=file_tree,
                    current_plan_json=_serialize_plan(plan),
                    feedback=plan.pending_feedback,
                )
            else:
                result = llm.generate_test_plan(
                    name=requirement.name,
                    description=requirement.description,
                    sibling_names=sibling_names,
                    test_env_content=test_env_content,
                    readme=readme,
                    file_tree=file_tree,
                )

            # The LLM loop is the long wait — the row may have been failed
            # (sprint finished) or reset meanwhile. Re-read the status and
            # discard a stale result rather than overwrite it.
            with session.no_autoflush:
                current_status = session.exec(
                    select(TestPlan.status).where(TestPlan.id == test_plan_id)
                ).one_or_none()
            if current_status != TestPlanStatus.GENERATING:
                logger.info(
                    "Test plan %d changed to '%s' mid-generation — discarding result",
                    test_plan_id,
                    current_status,
                )
                session.rollback()
                return

            plan.complexity = result.complexity
            plan.summary = result.summary
            plan.cases.clear()
            for position, case in enumerate(result.cases):
                plan.cases.append(
                    TestCase(
                        test_plan_id=plan.id,
                        position=position,
                        title=case.title,
                        preconditions=case.preconditions,
                        steps="\n".join(case.steps),
                        expected_result=case.expected_result,
                        case_type=case.case_type,
                        priority=case.priority,
                    )
                )
            if is_revision:
                plan.revision_count += 1
                plan.pending_feedback = None
            plan.status = TestPlanStatus.DRAFT
            plan.retry_count = 0
            plan.last_heartbeat = None
            plan.updated_at = _now()
            session.add(plan)
            session.commit()
            logger.info(
                "Test plan %d generated → %s (%d cases)",
                test_plan_id,
                plan.status,
                len(result.cases),
            )
        except Exception as exc:
            # Never re-raise: the DB retry counter, not RQ's failed registry,
            # is the recovery mechanism.
            logger.exception("Test plan generation failed for plan %d", test_plan_id)
            _record_failure(session, test_plan_id, exc)
