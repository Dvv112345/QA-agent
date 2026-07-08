"""Repo routes — create, list, deactivate repos and check README status."""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException
from sqlmodel import Session, select

from backend.database import get_session
from backend.models.repo import Repo
from backend.models.sprint import Sprint
from backend.models.types import ReadmeStatusResponse, RepoResponse
from backend.utils.auth import verify_auth
from backend.utils.crypto import decrypt_token, encrypt_token
from backend.utils.github_utils import (
    GitHubError,
    check_readme_exists,
    fetch_repo_metadata,
    parse_github_url,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_auth)])


@router.post("/repos", response_model=RepoResponse, status_code=201)
async def create_repo(
    github_url: str = Form(...),
    access_token: str | None = Form(None),
    session: Session = Depends(get_session),
) -> Repo:
    """Register a new GitHub repository.

    Validates the URL by calling the GitHub API.  If an ``access_token`` is
    provided it is encrypted before storage and never appears in responses or
    logs.
    """
    # ── Validate URL format ──────────────────────────────────────────
    try:
        owner, repo_name = parse_github_url(github_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Downgrade empty string to None (browsers may send empty form fields)
    token = access_token.strip() if access_token else None

    # ── Verify repo is accessible via GitHub API ─────────────────────
    try:
        metadata = await fetch_repo_metadata(owner, repo_name, token)
    except GitHubError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ── Encrypt token (if any) before storing ───────────────────────
    encrypted_token: str | None = None
    if token:
        try:
            encrypted_token = encrypt_token(token)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ── Persist ─────────────────────────────────────────────────────
    repo = Repo(
        github_link=github_url.strip().rstrip("/"),
        github_token=encrypted_token,
        name=metadata["full_name"],
        description=metadata.get("description"),
        active=True,
    )
    session.add(repo)
    session.commit()
    session.refresh(repo)

    logger.info(
        "Repo created: id=%d name=%s",
        repo.id,
        repo.name,
    )
    return repo


@router.get("/repos", response_model=list[RepoResponse])
async def list_repos(
    session: Session = Depends(get_session),
) -> list[Repo]:
    """List all active repos, newest first."""
    return list(
        session.exec(
            select(Repo).where(Repo.active == True).order_by(Repo.created_at.desc())  # noqa: E712
        ).all()
    )


@router.post("/repos/{repo_id}/deactivate")
async def deactivate_repo(
    repo_id: int,
    session: Session = Depends(get_session),
) -> dict[str, bool]:
    """Soft-delete a repo (set active=False, clear token).

    Blocked if any **active** sprint references this repo.
    """
    repo = session.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repo not found.")
    if not repo.active:
        raise HTTPException(status_code=422, detail="Repo is already deactivated.")

    active_sprint_count = session.exec(
        select(Sprint).where(
            Sprint.repo_id == repo_id,
            Sprint.active == True,  # noqa: E712
        )
    ).all()
    if active_sprint_count:
        count = len(active_sprint_count)
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot deactivate this repo: {count} active sprint(s) "
                f"are using it. Finish those sprints first."
            ),
        )

    repo.active = False
    repo.github_token = None
    session.add(repo)
    session.commit()

    logger.info("Repo deactivated: id=%d name=%s", repo.id, repo.name)
    return {"deactivated": True}


@router.get("/repos/{repo_id}/readme-status", response_model=ReadmeStatusResponse)
async def get_readme_status(
    repo_id: int,
    session: Session = Depends(get_session),
) -> dict[str, bool]:
    """Check whether the GitHub repository has a README file."""
    repo = session.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repo not found.")

    owner, repo_name = parse_github_url(repo.github_link)
    token = decrypt_token(repo.github_token) if repo.github_token else None

    try:
        has_readme = await check_readme_exists(owner, repo_name, token)
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"has_readme": has_readme}
