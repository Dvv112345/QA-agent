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
from backend.routes._common import get_sprint_or_404
from backend.services import issue_tracker
from backend.services.issue_tracker import TrackerConfig, TrackerError, TrackerUnavailableError
from backend.utils.auth import verify_auth
from backend.utils.crypto import decrypt_token, encrypt_token
from backend.utils.github_utils import parse_github_url

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_auth)])

_TOKEN_REQUIRED_ON_SWITCH = (
    "An API token is required when changing provider — "
    "the stored one belongs to the previous tracker."
)

_UNREADABLE_TOKEN = "The stored API token could not be read. Enter it again."


def _clean(value: str | None) -> str | None:
    """Downgrade a blank string to ``None`` (browsers send empty fields)."""
    stripped = (value or "").strip()
    return stripped or None


def _decrypt_or_http_error(ciphertext: str, subject: str) -> str:
    """Decrypt a stored token, mapping both failure modes to responses.

    The two are genuinely different: a missing or malformed
    ``ENCRYPTION_KEY`` is a server misconfiguration nobody can fix from
    the form (500), while ciphertext that will not decrypt under a valid
    key is a dead credential the user must simply re-enter (422).

    ``subject`` identifies the row in the log line; the token itself is
    never logged.
    """
    try:
        return decrypt_token(ciphertext)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # corrupted ciphertext — unusable, not fatal to the app
        logger.warning("%s could not be decrypted", subject)
        raise HTTPException(status_code=422, detail=_UNREADABLE_TOKEN) from exc


def _sprint_repo_target(sprint: Sprint) -> str:
    """``owner/repo`` for the sprint's own registered repository.

    Derived here rather than trusted from the form: the point of the
    option is that the application already knows which repository this
    sprint is about, and a value the browser sent back could name any
    other one.
    """
    repo = sprint.repo
    if repo is None:
        raise HTTPException(
            status_code=422, detail="This sprint has no registered repository to file into."
        )
    try:
        owner, name = parse_github_url(repo.github_link)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return f"{owner}/{name}"


def _repo_token(sprint: Sprint) -> str | None:
    """The sprint repo's stored access token, or ``None`` if it has none.

    A repo registered without a token is ordinary — public repositories
    need none to read — so the absence falls through to the remaining
    rules rather than failing the save here.
    """
    repo = sprint.repo
    if repo is None or not repo.github_token:
        return None
    return _decrypt_or_http_error(repo.github_token, f"Repo id={repo.id}: stored access token")


def _resolve_token(
    payload: IssueTrackerConfigRequest,
    existing: IssueTrackerConfig | None,
    sprint: Sprint,
) -> str:
    """The plaintext token this save should verify and store.

    Four rules, first match wins:

    1. a token typed into the form — explicit always beats derived;
    2. the sprint repo's own token, when ``use_sprint_repo`` asked for it.
       Above rule 3 on purpose: ticking the box and clearing the field is
       how a user moves an existing connection onto the repo's token, and
       it is also what makes a Jira→GitHub switch work without retyping,
       since this token is GitHub's by construction rather than the
       previous tracker's;
    3. the stored one on a **same-provider** edit — re-entering a secret
       to change a project key is the kind of friction that gets a token
       pasted into a chat window.  A Jira API token is meaningless to
       GitHub, so reusing it across a switch would verify nothing;
    4. nothing left to try.
    """
    supplied = _clean(payload.api_token)
    if supplied:
        return supplied
    if payload.use_sprint_repo:
        from_repo = _repo_token(sprint)
        if from_repo:
            return from_repo
    if existing is None:
        raise HTTPException(status_code=422, detail="An API token is required.")
    if existing.provider != payload.provider:
        raise HTTPException(status_code=422, detail=_TOKEN_REQUIRED_ON_SWITCH)
    return _decrypt_or_http_error(
        existing.api_token, f"Sprint id={existing.sprint_id}: stored tracker token"
    )


def _resolve_target(payload: IssueTrackerConfigRequest, sprint: Sprint) -> str | None:
    """Where findings go, before the provider-specific checks see it.

    Resolved ahead of ``_validate_provider_fields`` so a form that left
    the repository blank because the box was ticked is validated on the
    derived ``owner/repo`` — one shape check, not two.
    """
    if not payload.use_sprint_repo:
        return _clean(payload.target)
    if payload.provider != IssueTrackerProvider.GITHUB:
        raise HTTPException(
            status_code=422,
            detail="Using this sprint's repository applies to GitHub Issues only.",
        )
    return _sprint_repo_target(sprint)


def _validate_provider_fields(
    payload: IssueTrackerConfigRequest, target: str | None
) -> TrackerConfig:
    """Reject a payload whose provider-specific fields are missing.

    Checked here rather than in the schema because which fields are
    required depends on the provider: declaring them all optional and
    validating the *combination* is what lets the error name the field
    instead of reading as a malformed request.
    """
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
    sprint = get_sprint_or_404(session, sprint_id)
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

    ``use_sprint_repo`` files into the sprint's own registered repository:
    the target is derived from ``Repo.github_link`` and a blank token
    falls back to ``Repo.github_token``.  Both are resolved into an
    ordinary config here — the token is verified and re-encrypted exactly
    like a typed one, so what lands in the row is a **copy** taken at save
    time.  Rotating the repo's token later does not follow through, which
    is the price of leaving every export path untouched.

    Already-filed findings are deliberately untouched by an edit.  Their
    ``tracker_issue_url`` still points where they were actually filed,
    and their ``tracker_target`` keeps them out of the new tracker's
    de-duplication window.
    """
    sprint = get_sprint_or_404(session, sprint_id)
    existing = sprint.issue_tracker

    config = _validate_provider_fields(payload, _resolve_target(payload, sprint))
    token = _resolve_token(payload, existing, sprint)
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
    sprint = get_sprint_or_404(session, sprint_id)
    if sprint.issue_tracker is None:
        raise HTTPException(status_code=404, detail="No issue tracker is connected.")
    session.delete(sprint.issue_tracker)
    session.commit()
    logger.info("Sprint id=%d: issue tracker disconnected", sprint_id)
