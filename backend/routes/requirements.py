"""Requirement routes — batch create, PRD upload, list/poll, and per-requirement transitions."""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session, select, update

from backend.config import MAX_PRD_REQUIREMENTS, MAX_UPLOAD_SIZE_MB, PRD_MAX_CHARS
from backend.database import get_session
from backend.models.database import Requirement, RequirementStatus, Sprint
from backend.models.types import (
    RequirementAnswerRequest,
    RequirementCreateRequest,
    RequirementEditRequest,
    RequirementResponse,
)
from backend.services import invalidation, llm
from backend.services.llm import LLMError
from backend.services.queue import get_queue_service
from backend.services.storage import StorageService
from backend.utils.auth import verify_auth
from backend.utils.prd_utils import PrdExtractionError, extract_prd_text
from backend.utils.readme_utils import resolve_readme

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_auth)])


def _get_sprint_or_404(session: Session, sprint_id: int) -> Sprint:
    sprint = session.get(Sprint, sprint_id)
    if sprint is None:
        raise HTTPException(status_code=404, detail="Sprint not found.")
    return sprint


def _get_requirement_or_404(session: Session, requirement_id: int) -> Requirement:
    """Fetch a live requirement, 404ing on one the user has deleted.

    An archived row is still in the table, so a bare ``session.get`` would
    happily hand back a requirement the user removed and let every
    per-requirement route mutate it. Deleted means gone to every caller
    except the archive machinery itself.
    """
    requirement = session.get(Requirement, requirement_id)
    if requirement is None or requirement.archived:
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
    # A new requirement may need access the confirmed description never
    # covered, so the environment goes back for re-checking — in the same
    # transaction as the insert.
    invalidation.invalidate_for_requirement_add(session, sprint)
    session.commit()
    for row in rows:
        session.refresh(row)

    _enqueue_analysis(session, rows)

    logger.info("Created %d requirements for sprint id=%d", len(rows), sprint_id)
    return rows


@router.post(
    "/sprints/{sprint_id}/requirements/from-prd",
    response_model=list[RequirementResponse],
    status_code=201,
)
async def create_requirements_from_prd(
    sprint_id: int,
    prd_file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> list[Requirement]:
    """Split an uploaded PRD into requirements, replacing any prior PRD rows.

    Everything that can fail — file validation, text extraction, the LLM
    split — happens before the delete-and-replace transaction, so a failed
    upload never touches the existing requirements.  Manually entered rows
    are never touched either way.
    """
    sprint = _get_sprint_or_404(session, sprint_id)
    _ensure_sprint_active(sprint)

    filename = prd_file.filename or ""
    # Read one byte past the cap so an oversized body is rejected without
    # ever materialising more than the cap in memory.
    max_bytes = MAX_UPLOAD_SIZE_MB * 1024 * 1024
    content = prd_file.file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"PRD file exceeds the {MAX_UPLOAD_SIZE_MB} MB upload limit.",
        )

    try:
        # PDF/DOCX parsing can take seconds — keep it off the event loop.
        prd_text = await asyncio.to_thread(extract_prd_text, filename, content)
    except PrdExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if len(prd_text) > PRD_MAX_CHARS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"PRD text is {len(prd_text)} characters — the limit is "
                f"{PRD_MAX_CHARS}. Trim the document or split it into parts."
            ),
        )

    readme = await resolve_readme(sprint)
    file_tree = sprint.repo.file_tree if sprint.repo else None
    try:
        result = await asyncio.to_thread(llm.split_prd, prd_text, readme, file_tree)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    items = result.requirements
    if not items:
        raise HTTPException(
            status_code=422,
            detail="No requirements could be found in this document — is it a PRD?",
        )
    if len(items) > MAX_PRD_REQUIREMENTS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The PRD produced {len(items)} requirements — the limit is "
                f"{MAX_PRD_REQUIREMENTS}. Split the document into smaller parts."
            ),
        )

    # Replace the previous upload's rows and insert the new split in one
    # transaction. This is simultaneously a bulk delete and a bulk add, so
    # both cascades apply — routed through the invalidation module rather
    # than hand-rolled, since the archive-vs-hard-delete rule must match the
    # single-row route exactly.
    superseded = list(
        session.exec(
            select(Requirement).where(
                Requirement.sprint_id == sprint_id,
                Requirement.from_prd == True,  # noqa: E712
                # Rows the user already deleted stay deleted — a re-upload
                # replaces the live PRD set, not the archive.
                Requirement.archived == False,  # noqa: E712
            )
        ).all()
    )
    for row in superseded:
        invalidation.invalidate_for_requirement_delete(session, row)
    invalidation.invalidate_for_requirement_add(session, sprint)
    rows = [
        Requirement(
            sprint_id=sprint_id,
            name=item.name,
            description=item.description,
            original_description=item.description,
            from_prd=True,
        )
        for item in items
    ]
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)

    _enqueue_analysis(session, rows)

    try:
        StorageService().store_prd(content, sprint.directory, filename)
    except Exception as exc:
        logger.warning("Sprint id=%d: PRD storage failed: %s", sprint_id, exc)

    logger.info("Sprint id=%d: PRD split into %d requirements", sprint_id, len(rows))
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
            .where(
                Requirement.sprint_id == sprint_id,
                Requirement.archived == False,  # noqa: E712
            )
            .order_by(Requirement.created_at, Requirement.id)
        ).all()
    )


@router.post(
    "/sprints/{sprint_id}/requirements/confirm-all",
    response_model=list[RequirementResponse],
)
async def confirm_all_requirements(
    sprint_id: int,
    session: Session = Depends(get_session),
) -> list[Requirement]:
    """Confirm every requirement currently eligible (ready or needs_clarification).

    Ineligible rows (still analyzing, already confirmed, failed, …) are left
    untouched — same idempotent-skip semantics as ``generate_test_plans``.
    """
    sprint = _get_sprint_or_404(session, sprint_id)
    _ensure_sprint_active(sprint)

    session.exec(
        update(Requirement)
        .where(
            Requirement.sprint_id == sprint_id,
            Requirement.archived == False,  # noqa: E712
            Requirement.status.in_(  # type: ignore[attr-defined]
                [RequirementStatus.NEEDS_CLARIFICATION, RequirementStatus.READY]
            ),
        )
        .values(status=RequirementStatus.CONFIRMED, updated_at=datetime.now(timezone.utc))
    )
    session.commit()

    return list(
        session.exec(
            select(Requirement)
            .where(
                Requirement.sprint_id == sprint_id,
                Requirement.archived == False,  # noqa: E712
            )
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
    if requirement.clarification_cap_reached:
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
        RequirementStatus.CONFIRMED,
    ):
        raise HTTPException(
            status_code=422,
            detail="Only requirements awaiting clarification, ready, or confirmed can be edited.",
        )
    description = body.description.strip()
    if not description:
        raise HTTPException(status_code=422, detail="Description cannot be empty.")
    if description != requirement.description:
        # Editing the text invalidates everything written against the old
        # text: the plan goes, and the environment returns for re-checking.
        # Staged on this session so the edit and its cascade commit together.
        # Skipped when the text is unchanged, matching the environment path —
        # resubmitting identical text must not destroy an approved plan.
        invalidation.invalidate_for_requirement_change(session, requirement)

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
    # Removes its plan; the environment stays confirmed (removal can only
    # shrink what needs access). Archived rather than deleted when runs
    # reference it — see services/invalidation.py.
    invalidation.invalidate_for_requirement_delete(session, requirement)
    session.commit()
    logger.info("Requirement deleted: id=%d", requirement_id)
