"""Sprint routes — create, list, detail, and finish sprints."""

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from backend.config import MAX_UPLOAD_SIZE_MB, STORAGE_LOCATION
from backend.database import get_session
from backend.models.database import (
    SPRINT_FINISHED_ERROR,
    Repo,
    Requirement,
    RequirementStatus,
    Sprint,
    TestExecution,
    TestExecutionStatus,
    TestPlan,
    TestPlanStatus,
    TestRun,
)
from backend.models.types import (
    SprintResponse,
    SprintUpdateRequest,
)
from backend.services.storage import StorageService
from backend.utils.auth import verify_auth
from backend.utils.crypto import decrypt_token
from backend.utils.github_utils import (
    GitHubError,
    download_readme,
    fetch_file_tree,
    fetch_repo_metadata,
    parse_github_url,
)
from backend.utils.sprint_utils import generate_sprint_directory

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_auth)])

# Allowed extensions for uploaded README files.
_README_EXTENSIONS = {".md", ".markdown"}


def _validate_readme_file(readme_file: UploadFile) -> bytes:
    """Validate a README file and return its content as bytes."""
    # Extension check
    _, ext = os.path.splitext(readme_file.filename or "")
    if ext.lower() not in _README_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"README file must be a .md or .markdown file, got {ext or 'none'}.",
        )

    # Read one byte past the cap so an oversized body is rejected without
    # ever materialising more than the cap in memory.
    max_bytes = MAX_UPLOAD_SIZE_MB * 1024 * 1024
    content = readme_file.file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"README file exceeds the {MAX_UPLOAD_SIZE_MB} MB upload limit.",
        )

    # UTF-8 validation
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=422,
            detail="README file is not valid UTF-8.",
        ) from None

    return content


@router.post("/sprints", response_model=SprintResponse, status_code=201)
async def create_sprint(
    name: str = Form(...),
    repo_id: int = Form(...),
    readme_file: UploadFile | None = File(None),
    session: Session = Depends(get_session),
) -> Sprint:
    """Create a new sprint linked to a repo.

    Auto-downloads the README from GitHub if available, or requires a user
    upload if the repo has no README.
    """
    # ── Validate name ─────────────────────────────────────────────────
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Sprint name is required.")

    # ── Verify repo exists and is active ──────────────────────────────
    repo = session.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repo not found.")
    if not repo.active:
        raise HTTPException(
            status_code=422, detail="Cannot create a sprint for a deactivated repo."
        )

    # ── Refresh repo metadata from GitHub ─────────────────────────────
    owner, repo_name = parse_github_url(repo.github_link)
    token = decrypt_token(repo.github_token) if repo.github_token else None

    try:
        metadata = await fetch_repo_metadata(owner, repo_name, token)
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    repo.name = metadata["full_name"]
    repo.description = metadata.get("description")

    # ── Refresh repo file tree (best-effort LLM prompt context) ──────
    try:
        repo.file_tree = await fetch_file_tree(owner, repo_name, metadata["default_branch"], token)
    except GitHubError as exc:
        logger.warning("Sprint '%s': file tree refresh failed: %s", name, exc)

    session.add(repo)

    # ── Resolve README ────────────────────────────────────────────────
    # Priority: user upload > GitHub download > error
    readme_bytes: bytes | None = None

    if readme_file is not None and readme_file.filename:
        # User provided a file — validate and use it (overrides GitHub)
        readme_bytes = _validate_readme_file(readme_file)
        logger.info("Sprint '%s': using user-provided README", name)
    else:
        # No user file — download from GitHub (single API call)
        try:
            readme_text = await download_readme(owner, repo_name, token)
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if readme_text is not None:
            readme_bytes = readme_text.encode("utf-8")
            logger.info("Sprint '%s': downloaded README from GitHub", name)
        else:
            raise HTTPException(
                status_code=422,
                detail=("This repository does not have a README.md file. Please upload one."),
            )

    # ── Generate unique directory ─────────────────────────────────────
    directory, _dir_path = generate_sprint_directory(session, STORAGE_LOCATION)

    # ── Save README to disk ───────────────────────────────────────────
    storage = StorageService()
    if storage.offline and readme_bytes:
        storage.store_readme(readme_bytes, directory)

    # ── Persist sprint ────────────────────────────────────────────────
    sprint = Sprint(
        name=name,
        repo_id=repo_id,
        active=True,
        directory=directory,
    )
    session.add(sprint)
    session.commit()
    session.refresh(sprint)

    # Re-fetch to load the relationship
    sprint = session.get(Sprint, sprint.id)
    logger.info("Sprint created: id=%d name=%s repo=%s", sprint.id, sprint.name, repo.name)
    return sprint


@router.get("/sprints", response_model=list[SprintResponse])
async def list_sprints(
    offset: int = 0,
    limit: int = 100,
    session: Session = Depends(get_session),
) -> list[Sprint]:
    """List sprints — active first, newest first within each group."""
    return list(
        session.exec(
            select(Sprint)
            # The computed SprintResponse flags touch these relationships on
            # every row — eager-load them to avoid per-row lazy queries.
            .options(
                selectinload(Sprint.requirements).selectinload(Requirement.test_plan),
                selectinload(Sprint.test_environment),
                selectinload(Sprint.test_runs).selectinload(TestRun.executions),
            )
            .order_by(Sprint.active.desc(), Sprint.created_at.desc())  # noqa: E712
            .offset(offset)
            .limit(limit)
        ).all()
    )


@router.get("/sprints/{sprint_id}", response_model=SprintResponse)
async def get_sprint(
    sprint_id: int,
    session: Session = Depends(get_session),
) -> Sprint:
    """Get a single sprint with its associated repo info."""
    sprint = session.get(Sprint, sprint_id)
    if sprint is None:
        raise HTTPException(status_code=404, detail="Sprint not found.")
    return sprint


@router.patch("/sprints/{sprint_id}", response_model=SprintResponse)
async def finish_sprint(
    sprint_id: int,
    body: SprintUpdateRequest,
    session: Session = Depends(get_session),
) -> Sprint:
    """Finish a sprint (set active=False).

    Only valid when transitioning from active to finished.
    """
    sprint = session.get(Sprint, sprint_id)
    if sprint is None:
        raise HTTPException(status_code=404, detail="Sprint not found.")

    if body.active is not False:
        raise HTTPException(
            status_code=422,
            detail="Only transitioning active=False is supported.",
        )
    if not sprint.active:
        raise HTTPException(status_code=422, detail="Sprint is already finished.")

    sprint.active = False
    session.add(sprint)

    # ── Fail requirements still awaiting analysis ─────────────────────
    # Analysis on a finished sprint would only mutate cards the user can
    # no longer act on, so mark in-progress rows failed in the same commit.
    in_progress = session.exec(
        select(Requirement).where(
            Requirement.sprint_id == sprint_id,
            Requirement.status.in_(  # type: ignore[attr-defined]
                [RequirementStatus.PENDING, RequirementStatus.ANALYZING]
            ),
        )
    ).all()
    for requirement in in_progress:
        requirement.status = RequirementStatus.FAILED
        requirement.error = SPRINT_FINISHED_ERROR
        requirement.last_heartbeat = None
        requirement.pending_answer = None
        requirement.updated_at = datetime.now(timezone.utc)
        session.add(requirement)

    # ── Fail test plans still awaiting generation (same rationale) ────
    in_progress_plans = session.exec(
        select(TestPlan)
        .join(Requirement, TestPlan.requirement_id == Requirement.id)  # type: ignore[arg-type]
        .where(
            Requirement.sprint_id == sprint_id,
            TestPlan.status.in_(  # type: ignore[attr-defined]
                [TestPlanStatus.PENDING, TestPlanStatus.GENERATING]
            ),
        )
    ).all()
    for plan in in_progress_plans:
        plan.status = TestPlanStatus.FAILED
        plan.error = SPRINT_FINISHED_ERROR
        plan.last_heartbeat = None
        plan.pending_feedback = None
        plan.updated_at = datetime.now(timezone.utc)
        session.add(plan)

    # ── Fail test executions still awaiting a run (same rationale) ────
    in_progress_executions = session.exec(
        select(TestExecution)
        .join(Requirement, TestExecution.requirement_id == Requirement.id)  # type: ignore[arg-type]
        .where(
            Requirement.sprint_id == sprint_id,
            TestExecution.status.in_(  # type: ignore[attr-defined]
                [TestExecutionStatus.PENDING, TestExecutionStatus.RUNNING]
            ),
        )
    ).all()
    for execution in in_progress_executions:
        execution.status = TestExecutionStatus.FAILED
        execution.error = SPRINT_FINISHED_ERROR
        execution.last_heartbeat = None
        execution.updated_at = datetime.now(timezone.utc)
        session.add(execution)

    session.commit()
    session.refresh(sprint)

    if in_progress:
        logger.info(
            "Sprint id=%d: %d in-progress requirements marked failed on finish",
            sprint_id,
            len(in_progress),
        )
    if in_progress_plans:
        logger.info(
            "Sprint id=%d: %d in-progress test plans marked failed on finish",
            sprint_id,
            len(in_progress_plans),
        )
    if in_progress_executions:
        logger.info(
            "Sprint id=%d: %d in-progress test executions marked failed on finish",
            sprint_id,
            len(in_progress_executions),
        )
    logger.info("Sprint finished: id=%d name=%s", sprint.id, sprint.name)
    return sprint
