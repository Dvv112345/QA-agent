"""Sprint routes — create, list, detail, and finish sprints."""

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from backend.config import STORAGE_LOCATION
from backend.database import get_session
from backend.models.database import (
    ExploratoryRun,
    ExploratorySession,
    Repo,
    Requirement,
    Sprint,
    TestExecution,
    TestRun,
)
from backend.models.types import (
    SprintMetricsResponse,
    SprintResponse,
    SprintUpdateRequest,
)
from backend.routes._common import get_sprint_or_404
from backend.services.qa_metrics import compute_sprint_metrics
from backend.services.reconciler import SWEEP_SPECS, fail_in_progress_rows
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
from backend.utils.upload_utils import read_upload_capped

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

    content = read_upload_capped(readme_file, label="README file")

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
    readme_user_provided = False

    if readme_file is not None and readme_file.filename:
        # User provided a file — validate and use it (overrides GitHub)
        readme_bytes = _validate_readme_file(readme_file)
        readme_user_provided = True
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
    directory, _dir_path = generate_sprint_directory(STORAGE_LOCATION)

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
        readme_user_provided=readme_user_provided,
    )
    session.add(sprint)
    session.commit()
    session.refresh(sprint)

    logger.info("Sprint created: id=%d name=%s repo=%s", sprint.id, sprint.name, repo.name)
    return sprint


# Everything `SprintResponse`'s computed flags touch. They are evaluated
# for every serialized sprint, so without these the list endpoint issues
# several queries per row and the detail endpoint one per requirement.
#
# `all_requirements` rather than `requirements`: the latter is the
# archived-filtering property over it and cannot be given to selectinload.
_SPRINT_LOAD_OPTIONS = (
    selectinload(Sprint.all_requirements).selectinload(Requirement.test_plan),
    selectinload(Sprint.test_environment),
    selectinload(Sprint.repo),
    # `has_test_runs` / `has_exploratory_runs` ask only whether the
    # collection is non-empty, so neither chain needs its children here.
    selectinload(Sprint.test_runs),
    selectinload(Sprint.exploratory_runs),
)


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
            .options(*_SPRINT_LOAD_OPTIONS)
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
    sprint = session.exec(
        select(Sprint).where(Sprint.id == sprint_id).options(*_SPRINT_LOAD_OPTIONS)
    ).one_or_none()
    if sprint is None:
        raise HTTPException(status_code=404, detail="Sprint not found.")
    return sprint


@router.get("/sprints/{sprint_id}/qa-metrics", response_model=SprintMetricsResponse)
async def get_sprint_qa_metrics(
    sprint_id: int,
    session: Session = Depends(get_session),
) -> SprintMetricsResponse:
    """How QA went for this sprint — a pure read, safe to poll.

    No LLM call and no write, which is what lets the test-runs page fetch
    it on the same 2.5 s interval as the run lists.  The aggregator itself
    never raises, so a metrics panel cannot take the page down with it.
    """
    sprint = session.exec(
        select(Sprint)
        .where(Sprint.id == sprint_id)
        # Both chains are walked in full for every counted run, so without
        # these the endpoint issues a query per execution and per session —
        # on an endpoint that is polled. Same treatment `list_sprints` gives
        # the computed sprint flags.
        .options(
            selectinload(Sprint.all_requirements),
            selectinload(Sprint.test_runs)
            .selectinload(TestRun.executions)
            .selectinload(TestExecution.cases),
            selectinload(Sprint.exploratory_runs)
            .selectinload(ExploratoryRun.sessions)
            .selectinload(ExploratorySession.findings),
        )
    ).first()
    if sprint is None:
        raise HTTPException(status_code=404, detail="Sprint not found.")

    return SprintMetricsResponse(**compute_sprint_metrics(sprint))


@router.patch("/sprints/{sprint_id}", response_model=SprintResponse)
async def finish_sprint(
    sprint_id: int,
    body: SprintUpdateRequest,
    session: Session = Depends(get_session),
) -> Sprint:
    """Finish a sprint (set active=False).

    Only valid when transitioning from active to finished.
    """
    sprint = get_sprint_or_404(session, sprint_id)

    if body.active is not False:
        raise HTTPException(
            status_code=422,
            detail="Only transitioning active=False is supported.",
        )
    if not sprint.active:
        raise HTTPException(status_code=422, detail="Sprint is already finished.")

    sprint.active = False
    session.add(sprint)

    # ── Fail everything still in progress, in this same commit ────────
    # Work on a finished sprint would only mutate rows the user can no
    # longer act on. The four row types are swept by the reconciler's own
    # specs, which already encode each one's statuses, its join to Sprint,
    # its pending-input field, and its child rows — so this is the same
    # sweep the reconciler runs, scoped to one sprint instead of to every
    # inactive one.
    #
    # Deliberately *not* filtered on `archived`: this is convergence, not a
    # user-facing view. An archived row left in-progress would sit there
    # forever, since the reconciler skips archived rows by design.
    now = datetime.now(timezone.utc)
    failed_counts: list[tuple[str, int]] = []
    for spec in SWEEP_SPECS:
        rows = fail_in_progress_rows(session, spec, Sprint.id == sprint_id, now)
        if rows:
            failed_counts.append((spec.label, len(rows)))

    session.commit()
    session.refresh(sprint)

    for label, count in failed_counts:
        logger.info(
            "Sprint id=%d: %d in-progress %s rows marked failed on finish",
            sprint_id,
            count,
            label.lower(),
        )
    logger.info("Sprint finished: id=%d name=%s", sprint.id, sprint.name)
    return sprint
