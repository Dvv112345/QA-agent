"""Test-plan routes — batch generate, list/poll, and per-plan state transitions."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from backend.database import get_session
from backend.models.database import (
    Requirement,
    Sprint,
    TestCase,
    TestPlan,
    TestPlanStatus,
)
from backend.models.types import (
    TestPlanEditRequest,
    TestPlanFeedbackRequest,
    TestPlanResponse,
)
from backend.services.queue import get_queue_service
from backend.utils.auth import verify_auth

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_auth)])

_VALID_PRIORITIES = {"high", "medium", "low"}
_VALID_COMPLEXITIES = {"low", "medium", "high"}


def _get_sprint_or_404(session: Session, sprint_id: int) -> Sprint:
    sprint = session.get(Sprint, sprint_id)
    if sprint is None:
        raise HTTPException(status_code=404, detail="Sprint not found.")
    return sprint


def _get_plan_or_404(session: Session, plan_id: int) -> TestPlan:
    plan = session.get(TestPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Test plan not found.")
    return plan


def _ensure_sprint_active(sprint: Sprint | None) -> None:
    if sprint is None or not sprint.active:
        raise HTTPException(
            status_code=422,
            detail="Sprint is finished — test plans can no longer be modified.",
        )


def _touch(plan: TestPlan) -> None:
    plan.updated_at = datetime.now(timezone.utc)


def _enqueue_plans(session: Session, rows: list[TestPlan]) -> None:
    """Best-effort enqueue after commit — failure is the reconciler's job.

    Successful enqueues persist the job id for the reconciler's dedup check.
    """
    queue_service = get_queue_service()
    enqueued = False
    for row in rows:
        job = queue_service.enqueue_test_plan(row.id)
        if job is not None:
            row.job_id = job.id
            session.add(row)
            enqueued = True
    if enqueued:
        session.commit()
        for row in rows:
            session.refresh(row)


def _sprint_plans(session: Session, sprint_id: int) -> list[TestPlan]:
    """All the sprint's plans, ordered by their requirement's creation order."""
    return list(
        session.exec(
            select(TestPlan)
            .join(Requirement, TestPlan.requirement_id == Requirement.id)  # type: ignore[arg-type]
            .where(Requirement.sprint_id == sprint_id)
            .order_by(Requirement.created_at, Requirement.id)
            .options(selectinload(TestPlan.cases), selectinload(TestPlan.requirement))
        ).all()
    )


@router.post("/sprints/{sprint_id}/test-plans/generate", response_model=list[TestPlanResponse])
async def generate_test_plans(
    sprint_id: int,
    session: Session = Depends(get_session),
) -> list[TestPlan]:
    """Create a pending plan per confirmed requirement (resets failed plans).

    Idempotent: requirements that already have a plan in any non-failed
    status are skipped, and the full plan list is returned either way.
    """
    sprint = _get_sprint_or_404(session, sprint_id)
    _ensure_sprint_active(sprint)
    if not sprint.requirements_locked:
        raise HTTPException(
            status_code=422,
            detail="Confirm the test environment before generating test plans.",
        )

    requirements = sorted(sprint.requirements, key=lambda r: (r.created_at, r.id))
    to_enqueue: list[TestPlan] = []
    for requirement in requirements:
        plan = requirement.test_plan
        if plan is None:
            plan = TestPlan(requirement_id=requirement.id)
            session.add(plan)
            to_enqueue.append(plan)
        elif plan.status == TestPlanStatus.FAILED:
            # One button rescues a partially failed batch (like Restart —
            # pending_feedback is kept so an interrupted revision resumes).
            plan.status = TestPlanStatus.PENDING
            plan.error = None
            plan.retry_count = 0
            _touch(plan)
            session.add(plan)
            to_enqueue.append(plan)

    session.commit()
    for plan in to_enqueue:
        session.refresh(plan)

    _enqueue_plans(session, to_enqueue)

    if to_enqueue:
        logger.info(
            "Sprint id=%d: %d test plans created/reset for generation", sprint_id, len(to_enqueue)
        )
    return _sprint_plans(session, sprint_id)


@router.get("/sprints/{sprint_id}/test-plans", response_model=list[TestPlanResponse])
async def list_test_plans(
    sprint_id: int,
    session: Session = Depends(get_session),
) -> list[TestPlan]:
    """List a sprint's test plans — this is the polling endpoint (plain DB read)."""
    _get_sprint_or_404(session, sprint_id)
    return _sprint_plans(session, sprint_id)


@router.post("/test-plans/{plan_id}/feedback", response_model=TestPlanResponse)
async def submit_feedback(
    plan_id: int,
    body: TestPlanFeedbackRequest,
    session: Session = Depends(get_session),
) -> TestPlan:
    """Store the user's feedback on a draft plan and queue an LLM revision."""
    plan = _get_plan_or_404(session, plan_id)
    _ensure_sprint_active(plan.requirement.sprint if plan.requirement else None)

    if plan.status != TestPlanStatus.DRAFT:
        raise HTTPException(status_code=422, detail="Only draft plans can receive feedback.")
    if plan.feedback_cap_reached:
        raise HTTPException(
            status_code=422,
            detail="Feedback limit reached — edit the plan directly to continue.",
        )
    feedback = body.feedback.strip()
    if not feedback:
        raise HTTPException(status_code=422, detail="Feedback cannot be empty.")

    plan.pending_feedback = feedback
    plan.status = TestPlanStatus.PENDING
    _touch(plan)
    session.add(plan)
    session.commit()
    session.refresh(plan)
    _enqueue_plans(session, [plan])
    return plan


@router.patch("/test-plans/{plan_id}", response_model=TestPlanResponse)
async def edit_test_plan(
    plan_id: int,
    body: TestPlanEditRequest,
    session: Session = Depends(get_session),
) -> TestPlan:
    """Directly edit a draft plan (no LLM, uncapped, stays draft)."""
    plan = _get_plan_or_404(session, plan_id)
    _ensure_sprint_active(plan.requirement.sprint if plan.requirement else None)

    if plan.status != TestPlanStatus.DRAFT:
        raise HTTPException(status_code=422, detail="Only draft plans can be edited.")

    if body.complexity not in _VALID_COMPLEXITIES:
        raise HTTPException(status_code=422, detail="complexity must be one of: low, medium, high.")
    if not body.cases:
        raise HTTPException(status_code=422, detail="At least one test case is required.")
    for case in body.cases:
        if not case.title.strip():
            raise HTTPException(status_code=422, detail="Every test case needs a non-empty title.")
        if not any(line.strip() for line in case.steps.splitlines()):
            raise HTTPException(
                status_code=422, detail="Every test case needs at least one non-empty step."
            )
        if not case.expected_result.strip():
            raise HTTPException(
                status_code=422, detail="Every test case needs a non-empty expected result."
            )
        if not case.case_type.strip():
            raise HTTPException(status_code=422, detail="Every test case needs a non-empty type.")
        if case.priority not in _VALID_PRIORITIES:
            raise HTTPException(
                status_code=422, detail="priority must be one of: high, medium, low."
            )

    plan.complexity = body.complexity
    plan.summary = body.summary
    plan.cases.clear()
    for position, case in enumerate(body.cases):
        # Stored steps stay canonical (one non-blank line per step) — blank
        # lines would otherwise leak into revision prompts via the
        # serialized plan JSON.
        steps = "\n".join(line.strip() for line in case.steps.splitlines() if line.strip())
        plan.cases.append(
            TestCase(
                test_plan_id=plan.id,
                position=position,
                title=case.title,
                preconditions=case.preconditions,
                steps=steps,
                expected_result=case.expected_result,
                case_type=case.case_type,
                priority=case.priority,
            )
        )
    # Direct edits deliberately do not touch revision_count — it counts
    # LLM feedback revisions only.
    _touch(plan)
    session.add(plan)
    session.commit()
    session.refresh(plan)
    return plan


@router.post("/test-plans/{plan_id}/approve", response_model=TestPlanResponse)
async def approve_test_plan(
    plan_id: int,
    session: Session = Depends(get_session),
) -> TestPlan:
    """Approve a draft plan (terminal — no unapprove, no regenerate)."""
    plan = _get_plan_or_404(session, plan_id)
    _ensure_sprint_active(plan.requirement.sprint if plan.requirement else None)

    if plan.status != TestPlanStatus.DRAFT:
        raise HTTPException(status_code=422, detail="Only draft plans can be approved.")

    plan.status = TestPlanStatus.APPROVED
    _touch(plan)
    session.add(plan)
    session.commit()
    session.refresh(plan)
    return plan


@router.post("/test-plans/{plan_id}/restart", response_model=TestPlanResponse)
async def restart_test_plan(
    plan_id: int,
    session: Session = Depends(get_session),
) -> TestPlan:
    """Restart generation of a failed plan (uncapped, user-initiated).

    ``pending_feedback`` is kept so an interrupted feedback revision resumes.
    """
    plan = _get_plan_or_404(session, plan_id)
    _ensure_sprint_active(plan.requirement.sprint if plan.requirement else None)

    if plan.status != TestPlanStatus.FAILED:
        raise HTTPException(status_code=422, detail="Only failed plans can be restarted.")

    plan.status = TestPlanStatus.PENDING
    plan.error = None
    plan.retry_count = 0
    _touch(plan)
    session.add(plan)
    session.commit()
    session.refresh(plan)
    _enqueue_plans(session, [plan])
    return plan
