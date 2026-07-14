"""Test environment access routes — submit/check, answer, and confirm.

One row per sprint, judged synchronously by the LLM: the check runs inside
the request (offloaded to a thread) and nothing is persisted when it fails,
so there is no queue, retry, or reconciler involvement.
"""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from backend.database import get_session
from backend.models.database import (
    RequirementStatus,
    Sprint,
    TestEnvironmentAccess,
    TestEnvironmentStatus,
)
from backend.models.types import (
    TestEnvironmentAnswerRequest,
    TestEnvironmentResponse,
    TestEnvironmentSubmitRequest,
)
from backend.services import llm
from backend.services.llm import LLMError
from backend.utils.auth import verify_auth
from backend.utils.readme_utils import resolve_readme

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_auth)])

_REQUIREMENTS_INCOMPLETE_ERROR = (
    "All requirements must be confirmed (and at least one must exist) "
    "before describing the test environment."
)


def _get_sprint_or_404(session: Session, sprint_id: int) -> Sprint:
    sprint = session.get(Sprint, sprint_id)
    if sprint is None:
        raise HTTPException(status_code=404, detail="Sprint not found.")
    return sprint


def _get_test_env_or_404(session: Session, te_id: int) -> TestEnvironmentAccess:
    test_env = session.get(TestEnvironmentAccess, te_id)
    if test_env is None:
        raise HTTPException(status_code=404, detail="Test environment submission not found.")
    return test_env


def _ensure_sprint_active(sprint: Sprint) -> None:
    if not sprint.active:
        raise HTTPException(
            status_code=422,
            detail="Sprint is finished — the test environment can no longer be modified.",
        )


def _ensure_requirements_complete(sprint: Sprint) -> None:
    if not sprint.requirements_complete:
        raise HTTPException(status_code=422, detail=_REQUIREMENTS_INCOMPLETE_ERROR)


def _touch(test_env: TestEnvironmentAccess) -> None:
    test_env.updated_at = datetime.now(timezone.utc)


def _confirmed_requirements(sprint: Sprint) -> list[tuple[str, str]]:
    """Plain (name, description) pairs — safe to hand across the thread boundary."""
    return [
        (r.name, r.description)
        for r in sorted(sprint.requirements, key=lambda r: (r.created_at, r.id))
        if r.status == RequirementStatus.CONFIRMED
    ]


async def _gather_context(sprint: Sprint) -> tuple[list[tuple[str, str]], str | None, str | None]:
    """Resolve every LLM prompt input on the event-loop thread (plain values only)."""
    readme = await resolve_readme(sprint)
    file_tree = sprint.repo.file_tree if sprint.repo else None
    return _confirmed_requirements(sprint), readme, file_tree


@router.get(
    "/sprints/{sprint_id}/test-environment",
    response_model=TestEnvironmentResponse,
)
async def get_test_environment(
    sprint_id: int,
    session: Session = Depends(get_session),
) -> TestEnvironmentAccess:
    """Fetch a sprint's test environment submission (readable on finished sprints)."""
    sprint = _get_sprint_or_404(session, sprint_id)
    if sprint.test_environment is None:
        raise HTTPException(
            status_code=404, detail="No test environment submission for this sprint."
        )
    return sprint.test_environment


@router.post(
    "/sprints/{sprint_id}/test-environment",
    response_model=TestEnvironmentResponse,
)
async def submit_test_environment(
    sprint_id: int,
    body: TestEnvironmentSubmitRequest,
    session: Session = Depends(get_session),
) -> TestEnvironmentAccess:
    """Create or update the access description and run a fresh sufficiency check."""
    sprint = _get_sprint_or_404(session, sprint_id)
    _ensure_sprint_active(sprint)
    _ensure_requirements_complete(sprint)

    test_env = sprint.test_environment
    if test_env is not None and test_env.status == TestEnvironmentStatus.CONFIRMED:
        raise HTTPException(
            status_code=422,
            detail="The test environment has been confirmed and can no longer be edited.",
        )
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="Description cannot be empty.")

    requirements, readme, file_tree = await _gather_context(sprint)
    try:
        result = await asyncio.to_thread(
            llm.check_test_environment, content, requirements, readme, file_tree
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if test_env is None:
        test_env = TestEnvironmentAccess(
            sprint_id=sprint_id,
            content=content,
            original_content=content,
        )
    else:
        test_env.content = content

    if result.sufficient:
        test_env.status = TestEnvironmentStatus.READY
        test_env.clarifying_question = None
    else:
        test_env.status = TestEnvironmentStatus.NEEDS_INFO
        test_env.clarifying_question = result.clarifying_question

    _touch(test_env)
    session.add(test_env)
    session.commit()
    session.refresh(test_env)
    logger.info("Test environment checked for sprint id=%d → %s", sprint_id, test_env.status)
    return test_env


@router.post("/test-environment/{te_id}/answer", response_model=TestEnvironmentResponse)
async def answer_test_environment(
    te_id: int,
    body: TestEnvironmentAnswerRequest,
    session: Session = Depends(get_session),
) -> TestEnvironmentAccess:
    """Answer the clarifying question — the LLM rewrites the text and re-judges."""
    test_env = _get_test_env_or_404(session, te_id)
    sprint = test_env.sprint
    _ensure_sprint_active(sprint)
    _ensure_requirements_complete(sprint)

    if test_env.status != TestEnvironmentStatus.NEEDS_INFO:
        raise HTTPException(
            status_code=422,
            detail="Only submissions awaiting more information can be answered.",
        )
    if test_env.clarification_cap_reached:
        raise HTTPException(
            status_code=422,
            detail="Clarification limit reached — edit the text directly to continue.",
        )
    answer = body.answer.strip()
    if not answer:
        raise HTTPException(status_code=422, detail="Answer cannot be empty.")

    requirements, readme, file_tree = await _gather_context(sprint)
    try:
        result = await asyncio.to_thread(
            llm.revise_test_environment,
            test_env.content,
            test_env.clarifying_question,
            answer,
            requirements,
            readme,
            file_tree,
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    test_env.content = result.rewritten_content
    test_env.revision_count += 1
    if result.sufficient:
        test_env.status = TestEnvironmentStatus.READY
        test_env.clarifying_question = None
    else:
        test_env.status = TestEnvironmentStatus.NEEDS_INFO
        test_env.clarifying_question = result.clarifying_question

    _touch(test_env)
    session.add(test_env)
    session.commit()
    session.refresh(test_env)
    logger.info("Test environment revised for sprint id=%d → %s", sprint.id, test_env.status)
    return test_env


@router.post("/test-environment/{te_id}/confirm", response_model=TestEnvironmentResponse)
async def confirm_test_environment(
    te_id: int,
    session: Session = Depends(get_session),
) -> TestEnvironmentAccess:
    """Finalize the access description (terminal) — locks the requirement set."""
    test_env = _get_test_env_or_404(session, te_id)
    sprint = test_env.sprint
    _ensure_sprint_active(sprint)

    if test_env.status != TestEnvironmentStatus.READY:
        raise HTTPException(
            status_code=422,
            detail="Only submissions judged sufficient can be confirmed.",
        )
    if not sprint.requirements_complete:
        raise HTTPException(
            status_code=422,
            detail="All requirements must be confirmed before finalizing the test environment.",
        )
    if test_env.requirements_stale:
        raise HTTPException(
            status_code=422,
            detail=(
                "Requirements changed since the last check — "
                "re-check the test environment before confirming."
            ),
        )

    test_env.status = TestEnvironmentStatus.CONFIRMED
    _touch(test_env)
    session.add(test_env)
    session.commit()
    session.refresh(test_env)
    logger.info("Test environment confirmed for sprint id=%d", sprint.id)
    return test_env
