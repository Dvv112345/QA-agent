"""Requirement routes — batch create, list/poll, and per-requirement state transitions."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.config import MAX_CLARIFICATION_ROUNDS
from backend.database import get_session
from backend.models.database import Requirement, RequirementStatus, Sprint
from backend.models.types import (
    RequirementAnswerRequest,
    RequirementCreateRequest,
    RequirementEditRequest,
    RequirementResponse,
)
from backend.services.queue import get_queue_service
from backend.utils.auth import verify_auth

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_auth)])


def _get_sprint_or_404(session: Session, sprint_id: int) -> Sprint:
    sprint = session.get(Sprint, sprint_id)
    if sprint is None:
        raise HTTPException(status_code=404, detail="Sprint not found.")
    return sprint


def _get_requirement_or_404(session: Session, requirement_id: int) -> Requirement:
    requirement = session.get(Requirement, requirement_id)
    if requirement is None:
        raise HTTPException(status_code=404, detail="Requirement not found.")
    return requirement


def _ensure_sprint_active(sprint: Sprint) -> None:
    if not sprint.active:
        raise HTTPException(
            status_code=422,
            detail="Sprint is finished — requirements can no longer be modified.",
        )


def _touch(requirement: Requirement) -> None:
    requirement.updated_at = datetime.now(timezone.utc)


def _enqueue_analysis(session: Session, rows: list[Requirement]) -> None:
    """Best-effort enqueue after commit — failure is the reconciler's job.

    Successful enqueues persist the job id for the reconciler's dedup check.
    """
    queue_service = get_queue_service()
    enqueued = False
    for row in rows:
        job = queue_service.enqueue_analysis(row.id)
        if job is not None:
            row.job_id = job.id
            session.add(row)
            enqueued = True
    if enqueued:
        session.commit()
        for row in rows:
            session.refresh(row)


@router.post(
    "/sprints/{sprint_id}/requirements",
    response_model=list[RequirementResponse],
    status_code=201,
)
async def create_requirements(
    sprint_id: int,
    body: list[RequirementCreateRequest],
    session: Session = Depends(get_session),
) -> list[Requirement]:
    """Create a batch of requirements for a sprint (all start ``pending``)."""
    sprint = _get_sprint_or_404(session, sprint_id)
    _ensure_sprint_active(sprint)

    if not body:
        raise HTTPException(status_code=422, detail="At least one requirement is required.")

    rows: list[Requirement] = []
    for item in body:
        name = item.name.strip()
        description = item.description.strip()
        if not name or not description:
            raise HTTPException(
                status_code=422,
                detail="Every requirement needs a non-empty name and description.",
            )
        rows.append(
            Requirement(
                sprint_id=sprint_id,
                name=name,
                description=description,
                original_description=description,
            )
        )

    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)

    _enqueue_analysis(session, rows)

    logger.info("Created %d requirements for sprint id=%d", len(rows), sprint_id)
    return rows


@router.get(
    "/sprints/{sprint_id}/requirements",
    response_model=list[RequirementResponse],
)
async def list_requirements(
    sprint_id: int,
    session: Session = Depends(get_session),
) -> list[Requirement]:
    """List a sprint's requirements — this is the polling endpoint (plain DB read)."""
    _get_sprint_or_404(session, sprint_id)
    return list(
        session.exec(
            select(Requirement)
            .where(Requirement.sprint_id == sprint_id)
            .order_by(Requirement.created_at, Requirement.id)
        ).all()
    )


@router.post("/requirements/{requirement_id}/answer", response_model=RequirementResponse)
async def answer_requirement(
    requirement_id: int,
    body: RequirementAnswerRequest,
    session: Session = Depends(get_session),
) -> Requirement:
    """Store the user's answer to a clarifying question and queue a revision."""
    requirement = _get_requirement_or_404(session, requirement_id)
    _ensure_sprint_active(requirement.sprint)

    if requirement.status != RequirementStatus.NEEDS_CLARIFICATION:
        raise HTTPException(
            status_code=422,
            detail="Only requirements awaiting clarification can be answered.",
        )
    if requirement.revision_count >= MAX_CLARIFICATION_ROUNDS:
        raise HTTPException(
            status_code=422,
            detail="Clarification limit reached — confirm the requirement or edit it manually.",
        )
    answer = body.answer.strip()
    if not answer:
        raise HTTPException(status_code=422, detail="Answer cannot be empty.")

    requirement.pending_answer = answer
    requirement.status = RequirementStatus.PENDING
    _touch(requirement)
    session.add(requirement)
    session.commit()
    session.refresh(requirement)
    _enqueue_analysis(session, [requirement])
    return requirement


@router.post("/requirements/{requirement_id}/confirm", response_model=RequirementResponse)
async def confirm_requirement(
    requirement_id: int,
    session: Session = Depends(get_session),
) -> Requirement:
    """Confirm a requirement as final (terminal for content)."""
    requirement = _get_requirement_or_404(session, requirement_id)
    _ensure_sprint_active(requirement.sprint)

    if requirement.status not in (
        RequirementStatus.NEEDS_CLARIFICATION,
        RequirementStatus.READY,
    ):
        raise HTTPException(
            status_code=422,
            detail="Only requirements awaiting clarification or ready can be confirmed.",
        )

    requirement.status = RequirementStatus.CONFIRMED
    _touch(requirement)
    session.add(requirement)
    session.commit()
    session.refresh(requirement)
    return requirement


@router.patch("/requirements/{requirement_id}", response_model=RequirementResponse)
async def edit_requirement(
    requirement_id: int,
    body: RequirementEditRequest,
    session: Session = Depends(get_session),
) -> Requirement:
    """Manually edit a requirement's description and queue re-analysis."""
    requirement = _get_requirement_or_404(session, requirement_id)
    _ensure_sprint_active(requirement.sprint)

    if requirement.status not in (
        RequirementStatus.NEEDS_CLARIFICATION,
        RequirementStatus.READY,
    ):
        raise HTTPException(
            status_code=422,
            detail="Only requirements awaiting clarification or ready can be edited.",
        )
    description = body.description.strip()
    if not description:
        raise HTTPException(status_code=422, detail="Description cannot be empty.")

    requirement.description = description
    requirement.clarifying_question = None
    requirement.pending_answer = None
    requirement.status = RequirementStatus.PENDING
    _touch(requirement)
    session.add(requirement)
    session.commit()
    session.refresh(requirement)
    _enqueue_analysis(session, [requirement])
    return requirement


@router.post("/requirements/{requirement_id}/restart", response_model=RequirementResponse)
async def restart_requirement(
    requirement_id: int,
    session: Session = Depends(get_session),
) -> Requirement:
    """Restart analysis of a failed requirement (uncapped, user-initiated)."""
    requirement = _get_requirement_or_404(session, requirement_id)
    _ensure_sprint_active(requirement.sprint)

    if requirement.status != RequirementStatus.FAILED:
        raise HTTPException(
            status_code=422,
            detail="Only failed requirements can be restarted.",
        )

    requirement.status = RequirementStatus.PENDING
    requirement.error = None
    requirement.retry_count = 0
    _touch(requirement)
    session.add(requirement)
    session.commit()
    session.refresh(requirement)
    _enqueue_analysis(session, [requirement])
    return requirement


@router.delete("/requirements/{requirement_id}", status_code=204)
async def delete_requirement(
    requirement_id: int,
    session: Session = Depends(get_session),
) -> None:
    """Remove a requirement from its sprint (allowed in every status)."""
    requirement = _get_requirement_or_404(session, requirement_id)
    _ensure_sprint_active(requirement.sprint)

    session.delete(requirement)
    session.commit()
    logger.info("Requirement deleted: id=%d", requirement_id)
