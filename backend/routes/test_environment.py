"""Test environment access routes — submit/check, answer, and confirm.

One row per sprint, judged synchronously by the LLM: the check runs inside
the request (offloaded to a thread) and nothing is persisted when it fails,
so there is no queue, retry, or reconciler involvement.
"""

import asyncio
import json
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
    TestEnvironmentVarsEditRequest,
)
from backend.routes._common import ensure_sprint_active, get_sprint_or_404
from backend.services import invalidation, llm
from backend.services.llm import LLMError
from backend.utils.auth import verify_auth
from backend.utils.readme_utils import resolve_readme

logger = logging.getLogger(__name__)

# Completes "Sprint is finished — {}." for every gate in this module.
_GATE_SUBJECT = "the test environment can no longer be modified"

router = APIRouter(dependencies=[Depends(verify_auth)])

_REQUIREMENTS_INCOMPLETE_ERROR = (
    "All requirements must be confirmed (and at least one must exist) "
    "before describing the test environment."
)


def _get_test_env_or_404(session: Session, te_id: int) -> TestEnvironmentAccess:
    test_env = session.get(TestEnvironmentAccess, te_id)
    if test_env is None:
        raise HTTPException(status_code=404, detail="Test environment submission not found.")
    return test_env


def _ensure_requirements_complete(sprint: Sprint) -> None:
    if not sprint.requirements_complete:
        raise HTTPException(status_code=422, detail=_REQUIREMENTS_INCOMPLETE_ERROR)


def _apply_check_result(
    session: Session,
    sprint: Sprint,
    test_env: TestEnvironmentAccess,
    content: str,
    env_vars_json: str | None,
) -> None:
    """Store a checked description with its variables, invalidating the old pair.

    **Both fields move together and neither may be written past this
    function.** The description is what plans were generated against; the
    variables are what runs actually execute against. Changing either without
    invalidating leaves plans describing an environment that no longer exists
    and runs reporting as current.

    Every path that can change either — a resubmission, and an LLM rewrite
    from answering a clarifying question — goes through here. They were
    hand-rolled separately at first, and each hand-rolled version omitted a
    different half of the rule.

    No-ops when both are unchanged. That comparison is a backstop, not the
    defence: it can only recognise sameness the LLM happened to produce, and
    `generate_env_vars` is free to word the same access description
    differently on any call. The Re-check button re-POSTs the *current*
    content, so what actually keeps it from destroying the sprint's plans is
    `_resolve_env_vars_json` declining to re-extract at all.
    """
    # Compared as decoded values, not as JSON text: `json.dumps` preserves
    # whatever key order the model happened to emit, so identical variables
    # would otherwise read as a change and destroy every plan in the sprint
    # on a Re-check — the exact thing this guard exists to prevent.
    new_vars = json.loads(env_vars_json) if env_vars_json else None
    if test_env.content == content and test_env.env_vars == new_vars:
        return
    test_env.content = content
    test_env.env_vars_json = env_vars_json
    test_env.content_revision += 1
    invalidation.invalidate_for_environment_change(session, sprint)


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


async def _extract_env_vars_json(
    sufficient: bool, content: str, readme: str | None, file_tree: str | None
) -> str | None:
    """Extract env vars when the description is sufficient; clear to None
    otherwise, so the row never describes stale, superseded content."""
    if not sufficient:
        return None
    try:
        vars_result = await asyncio.to_thread(llm.generate_env_vars, content, readme, file_tree)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # Sorted so the stored text is stable across calls; the
    # comparison in `_apply_check_result` decodes anyway.
    return json.dumps(vars_result.variables, sort_keys=True)


async def _resolve_env_vars_json(
    sufficient: bool,
    test_env: TestEnvironmentAccess | None,
    content: str,
    readme: str | None,
    file_tree: str | None,
) -> str | None:
    """Extraction for a resubmission — kept as-is when the text is identical.

    The variables are *derived from* the description, so byte-identical
    content already has its correct derivation stored and re-deriving it buys
    nothing.  It costs two things.

    First, plans. The Re-check button exists to re-run the sufficiency
    judgment after the requirement set moved; it re-POSTs the current text
    unchanged.  A fresh `generate_env_vars` on that text need not come back
    identical — one renamed key, one extra variable, one trailing slash — and
    any drift reads as a content change in `_apply_check_result`, which
    deletes every test plan in the sprint and marks every run outdated.  A
    button whose whole purpose is "nothing changed, re-verify" must not be
    able to do that, and comparing after the fact cannot prevent it.

    Second, and quieter: `PATCH /test-environment/{id}/env-vars` lets the user
    hand-correct a value the model got wrong.  Re-extracting would overwrite
    that correction with no warning and no record of it.

    An insufficient verdict still clears the variables, and genuinely new text
    still gets a fresh extraction — both flow through `_extract_env_vars_json`
    below.  So does a row that has none yet, which is reachable on unchanged
    text: a description judged insufficient before can come back sufficient
    once the requirements it is judged against have changed.
    """
    if (
        sufficient
        and test_env is not None
        and test_env.content == content
        and test_env.env_vars_json
    ):
        return test_env.env_vars_json
    return await _extract_env_vars_json(sufficient, content, readme, file_tree)


@router.get(
    "/sprints/{sprint_id}/test-environment",
    response_model=TestEnvironmentResponse,
)
async def get_test_environment(
    sprint_id: int,
    session: Session = Depends(get_session),
) -> TestEnvironmentAccess:
    """Fetch a sprint's test environment submission (readable on finished sprints)."""
    sprint = get_sprint_or_404(session, sprint_id)
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
    sprint = get_sprint_or_404(session, sprint_id)
    ensure_sprint_active(sprint, _GATE_SUBJECT)
    _ensure_requirements_complete(sprint)

    test_env = sprint.test_environment
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

    env_vars_json = await _resolve_env_vars_json(
        result.sufficient, test_env, content, readme, file_tree
    )

    if test_env is None:
        test_env = TestEnvironmentAccess(
            sprint_id=sprint_id,
            content=content,
            original_content=content,
            env_vars_json=env_vars_json,
        )
    else:
        _apply_check_result(session, sprint, test_env, content, env_vars_json)

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
    ensure_sprint_active(sprint, _GATE_SUBJECT)
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

    env_vars_json = await _extract_env_vars_json(
        result.sufficient, result.rewritten_content, readme, file_tree
    )

    # The rewrite is a content change like any other: plans written against
    # the old description go, and runs grounded in it become outdated.
    # Reachable with plans intact — a Re-check that comes back insufficient
    # leaves them in place (correctly, nothing changed yet), and answering
    # from there is what changes the text.
    _apply_check_result(session, sprint, test_env, result.rewritten_content, env_vars_json)
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


@router.patch("/test-environment/{te_id}/env-vars", response_model=TestEnvironmentResponse)
async def edit_test_environment_vars(
    te_id: int,
    body: TestEnvironmentVarsEditRequest,
    session: Session = Depends(get_session),
) -> TestEnvironmentAccess:
    """Directly correct the LLM-extracted variables (uncapped, no LLM call)."""
    test_env = _get_test_env_or_404(session, te_id)
    sprint = test_env.sprint
    ensure_sprint_active(sprint, _GATE_SUBJECT)

    if not body.variables:
        raise HTTPException(status_code=422, detail="At least one variable is required.")
    for key, value in body.variables.items():
        if not key.strip() or not value.strip():
            raise HTTPException(
                status_code=422, detail="Variable names and values cannot be blank."
            )

    new_json = json.dumps(body.variables, sort_keys=True)
    if body.variables != test_env.env_vars:
        test_env.env_vars_json = new_json
        # A variables edit changes what a run executes against, so it is a
        # content change for staleness purposes and every plan goes.
        test_env.content_revision += 1
        invalidation.invalidate_for_environment_change(session, sprint)
        if test_env.status == TestEnvironmentStatus.CONFIRMED:
            # Back for re-confirmation, but never to needs_info: no LLM call
            # ran, so there is no clarifying question to answer.
            test_env.status = TestEnvironmentStatus.READY

    # Deliberately no _touch(): `updated_at` on this row means "last LLM
    # check", and that is what `requirements_stale` compares against.
    # Stamping it here would silently clear a real staleness flag when no
    # check has actually happened.
    session.add(test_env)
    session.commit()
    session.refresh(test_env)
    logger.info("Test environment vars edited for sprint id=%d", sprint.id)
    return test_env


@router.post("/test-environment/{te_id}/confirm", response_model=TestEnvironmentResponse)
async def confirm_test_environment(
    te_id: int,
    session: Session = Depends(get_session),
) -> TestEnvironmentAccess:
    """Finalize the access description (terminal) — locks the requirement set."""
    test_env = _get_test_env_or_404(session, te_id)
    sprint = test_env.sprint
    ensure_sprint_active(sprint, _GATE_SUBJECT)

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
    if test_env.env_vars_json is None:
        # Should be unreachable — READY already implies a sufficient check
        # populated this — but guarded per this codebase's convention of
        # never trusting a supposedly-impossible state blindly.
        raise HTTPException(
            status_code=422,
            detail="Environment variables have not been extracted yet — resubmit.",
        )

    test_env.status = TestEnvironmentStatus.CONFIRMED
    _touch(test_env)
    session.add(test_env)
    session.commit()
    session.refresh(test_env)
    logger.info("Test environment confirmed for sprint id=%d", sprint.id)
    return test_env
