"""CI/CD export routes — the sprint's connection, and (later) the exports themselves.

One ``CicdConfig`` per sprint, verified against the live repository's
``permissions.push`` inside the request so a read-only token is refused
while the user is still looking at the form — not after an export has spent
an LLM call discovering it.

There is deliberately no ``target`` anywhere in this module: the destination
is always the sprint's own registered repository.  The application already
knows which repository a sprint is about, so a value the browser sent back
could only ever name a different one.

``ensure_sprint_active`` is deliberately absent too.  A finished sprint is
exactly when a team wants its verified scripts committed — the testing is
done, and the scripts are the artefact worth keeping.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from backend.database import get_session
from backend.models.database import CicdConfig, CicdProvider, Sprint
from backend.models.types import (
    CicdConfigRequest,
    CicdConfigResponse,
    CicdEligibilityResponse,
)
from backend.routes._common import get_sprint_or_404
from backend.services import cicd_eligibility
from backend.utils.auth import verify_auth
from backend.utils.crypto import decrypt_token, encrypt_token
from backend.utils.environment_utils import url_values
from backend.utils.github_utils import (
    GitHubError,
    GitHubUnavailableError,
    check_push_permission,
    parse_github_url,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_auth)])

_UNREADABLE_TOKEN = "The stored access token could not be read. Enter it again."

_NO_PUSH = (
    "This token can read the repository but cannot push to it. "
    "A token with write access to contents and pull requests is required."
)

_TOKEN_REQUIRED = (
    "A GitHub access token with write access to contents and pull requests is required."
)


def _clean(value: str | None) -> str | None:
    """Downgrade a blank string to ``None`` (browsers send empty fields)."""
    stripped = (value or "").strip()
    return stripped or None


def _decrypt_or_http_error(ciphertext: str, subject: str) -> str:
    """Decrypt a stored token, mapping both failure modes to responses.

    Same split the issue tracker makes, for the same reason: a missing or
    malformed ``ENCRYPTION_KEY`` is a server misconfiguration nobody can fix
    from the form (500), while ciphertext that will not decrypt under a
    valid key is a dead credential the user simply re-enters (422).
    """
    try:
        return decrypt_token(ciphertext)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # corrupted ciphertext — unusable, not fatal to the app
        logger.warning("%s could not be decrypted", subject)
        raise HTTPException(status_code=422, detail=_UNREADABLE_TOKEN) from exc


def sprint_repo_slug(sprint: Sprint) -> tuple[str, str]:
    """``(owner, repo)`` for the sprint's own registered repository."""
    repo = sprint.repo
    if repo is None:
        raise HTTPException(
            status_code=422, detail="This sprint has no registered repository to export to."
        )
    try:
        return parse_github_url(repo.github_link)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _repo_token(sprint: Sprint) -> str | None:
    """The sprint repo's stored access token, or ``None``.

    A repo registered without one is ordinary — public repositories need no
    token to read — so its absence falls through to the next rule rather
    than failing the save here.  It is unlikely to be *pushable* either,
    which is what verification is for.
    """
    repo = sprint.repo
    if repo is None or not repo.github_token:
        return None
    return _decrypt_or_http_error(repo.github_token, f"Repo id={repo.id}: stored access token")


def _resolve_token(payload: CicdConfigRequest, existing: CicdConfig | None, sprint: Sprint) -> str:
    """The plaintext token this save should verify and store.

    Three rules, first match wins: a token typed into the form, then the
    stored one, then the sprint repo's own.

    Unlike the issue tracker, a **provider switch does not invalidate the
    stored credential**.  Jenkins ships as a GitHub pull request exactly as
    GitHub Actions does, so both providers authenticate with a GitHub
    token; requiring the user to re-type one to change the CI-environment
    hint would be friction with no security benefit.  There is therefore no
    analogue of ``_TOKEN_REQUIRED_ON_SWITCH`` here.
    """
    supplied = _clean(payload.access_token)
    if supplied:
        return supplied
    if existing is not None:
        return _decrypt_or_http_error(
            existing.access_token, f"Sprint id={existing.sprint_id}: stored CI/CD token"
        )
    from_repo = _repo_token(sprint)
    if from_repo:
        return from_repo
    raise HTTPException(status_code=422, detail=_TOKEN_REQUIRED)


def _validate_provider(provider: str) -> str:
    if provider not in {CicdProvider.GITHUB_ACTIONS, CicdProvider.JENKINS}:
        raise HTTPException(status_code=422, detail=f"Unknown CI/CD provider: {provider!r}")
    return provider


@router.get("/sprints/{sprint_id}/cicd-config", response_model=CicdConfigResponse | None)
async def get_cicd_config(sprint_id: int, session: Session = Depends(get_session)):
    """The sprint's CI/CD connection, or ``null`` when there is none."""
    sprint = get_sprint_or_404(session, sprint_id)
    return sprint.cicd_config


@router.put("/sprints/{sprint_id}/cicd-config", response_model=CicdConfigResponse)
async def save_cicd_config(
    sprint_id: int,
    payload: CicdConfigRequest,
    session: Session = Depends(get_session),
):
    """Connect a CI/CD target, or edit an existing connection.

    Verification runs on **every** save, first connect and edit alike, and
    it asks the strongest question this application asks of any credential:
    can this token *push*.  A token that can read but not write produces an
    export that fails on its first write, long after an LLM call has been
    spent — so the check belongs here, where it costs one request.

    Nothing persists unless verification succeeds.
    """
    sprint = get_sprint_or_404(session, sprint_id)
    existing = sprint.cicd_config

    provider = _validate_provider(payload.provider)
    owner, repo_name = sprint_repo_slug(sprint)
    token = _resolve_token(payload, existing, sprint)

    try:
        can_push = await check_push_permission(owner, repo_name, token)
    except GitHubUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except GitHubError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not can_push:
        raise HTTPException(status_code=422, detail=_NO_PUSH)

    try:
        encrypted = encrypt_token(token)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    now = datetime.now(timezone.utc)
    if existing is None:
        existing = CicdConfig(sprint_id=sprint_id, provider=provider, access_token=encrypted)
        session.add(existing)
    # Assigned wholesale rather than field by field, so a provider switch
    # cannot leave anything behind from the previous one.
    existing.provider = provider
    existing.access_token = encrypted
    existing.ci_environment_hint = _clean(payload.ci_environment_hint)
    existing.verified_at = now
    existing.updated_at = now

    session.commit()
    session.refresh(existing)

    # The provider and the repository, never the token — the only line in
    # this module that mentions the connection at INFO.
    logger.info(
        "Sprint id=%d: CI/CD export connected to %s/%s (%s)", sprint_id, owner, repo_name, provider
    )
    return existing


@router.delete("/sprints/{sprint_id}/cicd-config", status_code=204)
async def delete_cicd_config(sprint_id: int, session: Session = Depends(get_session)):
    """Disconnect CI/CD export.

    Deliberately **not** blocked by an export in flight, for the reason the
    tracker's disconnect gives: that export fails on its first write with a
    mapped ``GitHubError``, which is a visible, recoverable outcome, and
    blocking would put a user-facing setting behind whatever a worker
    happens to be doing.
    """
    sprint = get_sprint_or_404(session, sprint_id)
    if sprint.cicd_config is None:
        raise HTTPException(status_code=404, detail="No CI/CD target is connected.")
    session.delete(sprint.cicd_config)
    session.commit()
    logger.info("Sprint id=%d: CI/CD export disconnected", sprint_id)


# ── Eligibility ───────────────────────────────────────────────────────


def env_var_name_split(sprint: Sprint) -> tuple[list[str], list[str]]:
    """Environment variable **names**, split into CI variables and CI secrets.

    A URL-valued variable becomes a plain CI variable; everything else
    becomes a secret.  Values are read here only to sort the names and are
    then discarded — nothing below this line ever sees one, and no response
    in this module carries one.

    That split is what lets the export page say up front which repository
    secrets the team will have to create, instead of the team learning it
    from a workflow that runs and fails.
    """
    test_env = sprint.test_environment
    env_vars = test_env.env_vars if test_env is not None else None
    if not env_vars:
        return [], []
    urls = url_values(env_vars)
    variables = [name for name, value in env_vars.items() if value in urls]
    secrets = [name for name, value in env_vars.items() if value not in urls]
    return variables, secrets


@router.get("/sprints/{sprint_id}/cicd-eligibility", response_model=CicdEligibilityResponse)
async def get_cicd_eligibility(sprint_id: int, session: Session = Depends(get_session)):
    """Every test case in the sprint, and whether it can be exported.

    Ineligible cases are listed with their reason rather than filtered out:
    "no script yet" and "out of date" imply different actions, and a case
    that simply vanished from the list is indistinguishable from a bug.
    """
    sprint = cicd_eligibility.load_sprint_for_eligibility(session, sprint_id)
    if sprint is None:
        raise HTTPException(status_code=404, detail="Sprint not found")

    entries = cicd_eligibility.case_entries(session, sprint)
    variables, secrets = env_var_name_split(sprint)
    return CicdEligibilityResponse(
        sprint_id=sprint_id,
        entries=entries,
        eligible_count=sum(1 for entry in entries if entry.eligible),
        stale_count=sum(1 for entry in entries if entry.reason == "stale"),
        no_script_count=sum(1 for entry in entries if entry.reason == "no_script"),
        variable_names=variables,
        secret_names=secrets,
    )
