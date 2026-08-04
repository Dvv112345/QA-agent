"""Issue-tracker configuration routes — get, create-or-edit, disconnect.

One row per sprint, verified against the live tracker inside the request
(offloaded to a thread, exactly like the test-environment check) so a
credential problem is reported while the user is still on the form rather
than as a ``tracker_error`` on a finding hours later.

Nothing here files an issue.  Export lives in the worker tasks and the
per-run retry routes; this module only decides *where* findings would go.
"""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from backend.database import get_session
from backend.models.database import IssueTrackerConfig, IssueTrackerProvider, Sprint
from backend.models.types import IssueTrackerConfigRequest, IssueTrackerConfigResponse
from backend.services import issue_tracker
from backend.services.issue_tracker import TrackerConfig, TrackerError, TrackerUnavailableError
from backend.utils.auth import verify_auth
from backend.utils.crypto import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_auth)])

_TOKEN_REQUIRED_ON_SWITCH = (
    "An API token is required when changing provider — "
    "the stored one belongs to the previous tracker."
)


def _get_sprint_or_404(session: Session, sprint_id: int) -> Sprint:
    sprint = session.get(Sprint, sprint_id)
    if sprint is None:
        raise HTTPException(status_code=404, detail="Sprint not found.")
    return sprint


def _clean(value: str | None) -> str | None:
    """Downgrade a blank string to ``None`` (browsers send empty fields)."""
    stripped = (value or "").strip()
    return stripped or None


def _resolve_token(payload: IssueTrackerConfigRequest, existing: IssueTrackerConfig | None) -> str:
    """The plaintext token this save should verify and store.

    Blank means "keep the stored one", which is the whole point of the
    rule: re-entering a secret to change a project key is the kind of
    friction that gets a token pasted into a chat window.  It applies
    **only** to a same-provider edit — a Jira API token is meaningless to
    GitHub, so silently reusing it across a switch would verify nothing
    and store a credential that can never work.
    """
    supplied = _clean(payload.api_token)
    if supplied:
        return supplied
    if existing is None:
        raise HTTPException(status_code=422, detail="An API token is required.")
    if existing.provider != payload.provider:
        raise HTTPException(status_code=422, detail=_TOKEN_REQUIRED_ON_SWITCH)
    try:
        return decrypt_token(existing.api_token)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # corrupted ciphertext — unusable, not fatal to the app
        logger.warning(
            "Sprint id=%s: stored tracker token could not be decrypted", existing.sprint_id
        )
        raise HTTPException(
            status_code=422, detail="The stored API token could not be read. Enter it again."
        ) from exc


def _validate_provider_fields(payload: IssueTrackerConfigRequest) -> TrackerConfig:
    """Reject a payload whose provider-specific fields are missing.

    Checked here rather than in the schema because which fields are
    required depends on the provider: declaring them all optional and
    validating the *combination* is what lets the error name the field
    instead of reading as a malformed request.
    """
    target = _clean(payload.target)
    if not target:
        raise HTTPException(status_code=422, detail="A project key or repository is required.")

    if payload.provider == IssueTrackerProvider.JIRA:
        base_url = _clean(payload.base_url)
        if not base_url:
            raise HTTPException(
                status_code=422,
                detail="A Jira site URL is required (for example https://your-team.atlassian.net).",
            )
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=422, detail="The Jira site URL must start with http:// or https://."
            )
        if not _clean(payload.account_email):
            raise HTTPException(
                status_code=422, detail="The Jira account email is required for API access."
            )
        if not _clean(payload.issue_type):
            raise HTTPException(
                status_code=422, detail="A Jira issue type is required (for example 'Bug')."
            )
        return TrackerConfig(
            provider=payload.provider,
            target=target,
            api_token="",  # filled by the caller — see _resolve_token
            base_url=base_url.rstrip("/"),
            account_email=_clean(payload.account_email),
            issue_type=_clean(payload.issue_type),
        )

    if payload.provider == IssueTrackerProvider.GITHUB:
        if len([part for part in target.split("/") if part]) != 2:
            raise HTTPException(
                status_code=422,
                detail="Enter the repository as 'owner/repo' (for example acme/shop).",
            )
        # Provider-irrelevant fields are dropped rather than carried, so a
        # stale Jira site can never linger on a GitHub config.
        return TrackerConfig(provider=payload.provider, target=target, api_token="")

    raise HTTPException(
        status_code=422, detail=f"Unknown issue tracker provider: {payload.provider!r}"
    )


@router.get("/sprints/{sprint_id}/issue-tracker", response_model=IssueTrackerConfigResponse | None)
async def get_issue_tracker(sprint_id: int, session: Session = Depends(get_session)):
    """The sprint's tracker connection, or ``null`` when there is none."""
    sprint = _get_sprint_or_404(session, sprint_id)
    return sprint.issue_tracker


@router.put("/sprints/{sprint_id}/issue-tracker", response_model=IssueTrackerConfigResponse)
async def save_issue_tracker(
    sprint_id: int,
    payload: IssueTrackerConfigRequest,
    session: Session = Depends(get_session),
):
    """Connect a tracker, or re-point an existing connection.

    Verification runs on **every** save, first connect and edit alike:
    the point of connecting is that findings will reach the tracker
    later, and the only moment a credential problem is cheap to fix is
    while the user is looking at the form.

    Already-filed findings are deliberately untouched by an edit.  Their
    ``tracker_issue_url`` still points where they were actually filed,
    and their ``tracker_target`` keeps them out of the new tracker's
    de-duplication window.
    """
    sprint = _get_sprint_or_404(session, sprint_id)
    existing = sprint.issue_tracker

    config = _validate_provider_fields(payload)
    token = _resolve_token(payload, existing)
    # Verified with the plaintext token; encrypted only once it works.
    config = TrackerConfig(
        provider=config.provider,
        target=config.target,
        api_token=token,
        base_url=config.base_url,
        account_email=config.account_email,
        issue_type=config.issue_type,
    )

    try:
        label = await asyncio.to_thread(issue_tracker.verify, config)
    except TrackerUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TrackerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        encrypted = encrypt_token(token)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    now = datetime.now(timezone.utc)
    if existing is None:
        existing = IssueTrackerConfig(
            sprint_id=sprint_id, api_token=encrypted, target=config.target
        )
        session.add(existing)
    else:
        existing.api_token = encrypted
    # Assigned wholesale rather than field by field so a provider switch
    # cannot leave a field behind: every one is written every time, and
    # the ones the new provider does not use were already resolved to
    # None by _validate_provider_fields.
    existing.provider = config.provider
    existing.target = config.target
    existing.base_url = config.base_url
    existing.account_email = config.account_email
    existing.issue_type = config.issue_type
    existing.verified_at = now
    existing.updated_at = now

    session.commit()
    session.refresh(existing)

    # `label` and never the token: this is the only line in the module
    # that mentions the connection at INFO.
    logger.info(
        "Sprint id=%d: issue tracker connected to %s (%s)", sprint_id, label, config.provider
    )
    return existing


@router.delete("/sprints/{sprint_id}/issue-tracker", status_code=204)
async def delete_issue_tracker(sprint_id: int, session: Session = Depends(get_session)):
    """Disconnect the tracker.

    Deliberately **not** blocked by a run in flight.  Blocking would put
    a user-facing setting behind whatever a worker happens to be doing,
    to prevent an outcome that is already handled: that run's export
    fails into ``tracker_error`` and its findings stay on the run page
    with a button to file them once a tracker is connected again.
    """
    sprint = _get_sprint_or_404(session, sprint_id)
    if sprint.issue_tracker is None:
        raise HTTPException(status_code=404, detail="No issue tracker is connected.")
    session.delete(sprint.issue_tracker)
    session.commit()
    logger.info("Sprint id=%d: issue tracker disconnected", sprint_id)
