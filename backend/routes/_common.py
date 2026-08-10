"""Lookups and gates every sprint-scoped router needs.

These two functions were previously copied into each route module — six
identical sprint lookups and five gates differing only in the noun at the end
of the message.  One copy each means the 404 wording and the finished-sprint
rule have a single home.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session

from backend.models.database import Sprint


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
