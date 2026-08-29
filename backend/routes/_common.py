"""Lookups, gates and preconditions shared between sprint-scoped routers.

Two kinds of thing live here, and they earn their place differently.

``get_sprint_or_404`` and ``ensure_sprint_active`` are needed by *every*
sprint-scoped router — six identical sprint lookups and five gates differing
only in the noun at the end of the message. One copy each means the 404
wording and the finished-sprint rule have a single home.

The three below are shared by exactly the **two browser-driven run modes**,
exploratory and nonfunctional. Two callers is a lower bar, and the reason it
is met here is that these encode a *decision* rather than a convenience: what
counts as a requirement ready to be run against, what counts as a usable test
environment, and what counts as a valid application URL. Two run modes
answering those differently would be a bug either way round.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException
from sqlmodel import Session

from backend.models.database import (
    Requirement,
    RequirementStatus,
    Sprint,
    TestEnvironmentStatus,
    TestPlanStatus,
)


def get_sprint_or_404(session: Session, sprint_id: int) -> Sprint:
    """Fetch a sprint, 404ing when it does not exist."""
    sprint = session.get(Sprint, sprint_id)
    if sprint is None:
        raise HTTPException(status_code=404, detail="Sprint not found.")
    return sprint


def ensure_sprint_active(sprint: Sprint | None, subject: str) -> None:
    """Refuse a write to a finished sprint.

    ``subject`` completes the sentence "Sprint is finished — {subject}." and
    so carries the whole predicate, not just a noun: the stages differ on
    whether the refused verb is "modified", "created", or "created or
    restarted".

    A missing sprint counts as inactive.  Callers that already hold a
    non-``None`` sprint are unaffected; those that reach one through an
    optional relationship get the gate without a separate ``None`` check.
    """
    if sprint is None or not sprint.active:
        raise HTTPException(
            status_code=422,
            detail=f"Sprint is finished — {subject}.",
        )


def resolve_requirement_for_run(sprint: Sprint, requirement_id: int) -> Requirement:
    """The precondition set every browser-driven run stage shares.

    A confirmed requirement with an approved plan — the gate exploratory
    and nonfunctional runs both open on, worded once so the two cannot
    drift into refusing for different reasons.
    """
    requirement = next((r for r in sprint.requirements if r.id == requirement_id), None)
    if requirement is None or requirement.status != RequirementStatus.CONFIRMED:
        raise HTTPException(
            status_code=422,
            detail=f"Requirement id {requirement_id} was not found or is not confirmed.",
        )
    if requirement.test_plan is None or requirement.test_plan.status != TestPlanStatus.APPROVED:
        raise HTTPException(
            status_code=422,
            detail=f"Requirement '{requirement.name}' does not have an approved test plan.",
        )
    return requirement


def resolve_confirmed_env_vars(sprint: Sprint) -> dict[str, str]:
    test_env = sprint.test_environment
    if test_env is None or test_env.status != TestEnvironmentStatus.CONFIRMED:
        raise HTTPException(
            status_code=422,
            detail="The test environment must be confirmed before exploratory testing.",
        )
    env_vars = test_env.env_vars
    if not env_vars:
        raise HTTPException(
            status_code=422,
            detail="No test environment variables are available for this sprint.",
        )
    return env_vars


def validate_url_vars(names: list[str], env_vars: dict[str, str], status_code: int) -> None:
    """Every nominated name must exist and hold an http(s) URL.

    Called with two different status codes: 502 when the model nominated
    them (malformed LLM output) and 422 when the client sent them back (bad
    input). The same names go through both, once each, on either run mode.
    """
    if not names:
        raise HTTPException(
            status_code=status_code,
            detail="No environment variable was nominated for the application URL.",
        )
    for name in names:
        if name not in env_vars:
            raise HTTPException(
                status_code=status_code,
                detail=f"Environment variable '{name}' does not exist in this sprint.",
            )
        parsed = urlparse(env_vars[name])
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(
                status_code=status_code,
                detail=f"Environment variable '{name}' does not hold an http(s) URL.",
            )
