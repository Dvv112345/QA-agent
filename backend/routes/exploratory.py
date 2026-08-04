"""Exploratory-testing routes — charter drafting, runs, session sheets, findings.

Charter generation is **synchronous** inside the request (one cheap LLM call,
no tool loop), following the ``from-prd`` and test-environment-check
precedents: ``LLMError`` maps to 502 and nothing is persisted.  Everything
long-running happens in the RQ task instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from backend.config import (
    EXPLORATORY_MAX_ACTIONS,
    EXPLORATORY_MAX_CHARTERS,
    EXPLORATORY_SECONDS_PER_ACTION,
)
from backend.database import get_session
from backend.models.database import (
    ExploratoryFinding,
    ExploratoryRun,
    ExploratoryRunStatus,
    ExploratorySession,
    FindingSeverity,
    FindingType,
    Requirement,
    RequirementStatus,
    SfdipotArea,
    Sprint,
    TestEnvironmentStatus,
    TestPlanStatus,
    outdated_restart_error,
)
from backend.models.types import (
    CharterDraft,
    ExploratoryCharterDraftResponse,
    ExploratoryCharterGenerateRequest,
    ExploratoryFindingResponse,
    ExploratoryRunCreateRequest,
    ExploratoryRunDetailResponse,
    ExploratoryRunResponse,
    ExploratorySessionResponse,
    ExploratorySessionSummaryResponse,
)
from backend.services import llm
from backend.services.finding_export import TRACKER_REQUIRED_ERROR
from backend.services.llm_prompts import TestCaseLike
from backend.services.queue import get_queue_service
from backend.utils.auth import verify_auth
from backend.utils.exploratory_utils import session_sheets
from backend.utils.readme_utils import refresh_file_tree, resolve_readme

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_auth)])


# ── lookups and guards ────────────────────────────────────────────────


def _get_sprint_or_404(session: Session, sprint_id: int) -> Sprint:
    sprint = session.get(Sprint, sprint_id)
    if sprint is None:
        raise HTTPException(status_code=404, detail="Sprint not found.")
    return sprint


def _get_run_or_404(session: Session, run_id: int) -> ExploratoryRun:
    run = session.exec(
        select(ExploratoryRun)
        .where(ExploratoryRun.id == run_id)
        .options(
            # Same reason as the scripted list: `outdated_reasons` walks
            # requirement → test_plan and sprint → test_environment, and this
            # endpoint polls every 2.5s.
            selectinload(ExploratoryRun.requirement).selectinload(Requirement.test_plan),
            selectinload(ExploratoryRun.sessions).selectinload(ExploratorySession.findings),
            selectinload(ExploratoryRun.sprint).selectinload(Sprint.test_environment),
        )
    ).one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Exploratory run not found.")
    return run


def _ensure_sprint_active(sprint: Sprint | None) -> None:
    if sprint is None or not sprint.active:
        raise HTTPException(
            status_code=422,
            detail="Sprint is finished — exploratory runs can no longer be created.",
        )


def _resolve_requirement(sprint: Sprint, requirement_id: int) -> Requirement:
    """The shared precondition set for drafting charters and starting a run."""
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


def _resolve_env_vars(sprint: Sprint) -> dict[str, str]:
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


def _validate_url_vars(names: list[str], env_vars: dict[str, str], status_code: int) -> None:
    """Every nominated name must exist and hold an http(s) URL.

    Used twice with different status codes: 502 when the model nominated them
    (malformed LLM output) and 422 when the client sent them back (bad input).
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


def _validate_charters(charters: list[CharterDraft]) -> None:
    """Validate the (possibly user-edited) charter list — 422, it's user input now."""
    if not charters:
        raise HTTPException(status_code=422, detail="At least one charter is required.")
    if len(charters) > EXPLORATORY_MAX_CHARTERS:
        raise HTTPException(
            status_code=422,
            detail=f"At most {EXPLORATORY_MAX_CHARTERS} charters are allowed per run.",
        )
    valid_areas = {area.value for area in SfdipotArea}
    for charter in charters:
        if not charter.charter.strip():
            raise HTTPException(status_code=422, detail="A charter cannot be blank.")
        for area in charter.sfdipot_areas:
            if area not in valid_areas:
                raise HTTPException(status_code=422, detail=f"Unknown SFDIPOT area: '{area}'.")


# ── response builders (aggregates computed here, never stored) ────────


def _finding_response(finding: ExploratoryFinding) -> ExploratoryFindingResponse:
    return ExploratoryFindingResponse(
        id=finding.id,
        position=finding.position,
        finding_type=finding.finding_type,
        severity=finding.severity,
        title=finding.title,
        steps_to_reproduce=finding.steps_to_reproduce,
        expected=finding.expected,
        actual=finding.actual,
        environment=finding.environment,
        has_screenshot=finding.screenshot_path is not None,
        created_at=finding.created_at,
    )


def _session_summary(session_row: ExploratorySession) -> ExploratorySessionSummaryResponse:
    return ExploratorySessionSummaryResponse(
        id=session_row.id,
        position=session_row.position,
        charter=session_row.charter,
        sfdipot_areas=session_row.sfdipot_areas,
        status=session_row.status,
        actions_used=session_row.actions_used,
        stop_reason=session_row.stop_reason,
        error=session_row.error,
        finding_count=len(session_row.findings),
        updated_at=session_row.updated_at,
    )


def _finding_counts(run: ExploratoryRun) -> tuple[int, int, int]:
    bugs = issues = high = 0
    for session_row in run.sessions:
        for finding in session_row.findings:
            if finding.finding_type == FindingType.BUG:
                bugs += 1
            elif finding.finding_type == FindingType.ISSUE:
                issues += 1
            if finding.severity == FindingSeverity.HIGH:
                high += 1
    return bugs, issues, high


def _run_response(run: ExploratoryRun) -> ExploratoryRunResponse:
    bugs, issues, high = _finding_counts(run)
    return ExploratoryRunResponse(
        id=run.id,
        sprint_id=run.sprint_id,
        requirement_id=run.requirement_id,
        requirement_name=run.requirement_name,
        status=run.status,
        summary=run.summary,
        error=run.error,
        outdated_reasons=run.outdated_reasons,
        requirement_deleted=run.requirement_deleted,
        session_count=len(run.sessions),
        bug_count=bugs,
        issue_count=issues,
        high_severity_count=high,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _run_detail(run: ExploratoryRun) -> ExploratoryRunDetailResponse:
    bugs, issues, high = _finding_counts(run)
    return ExploratoryRunDetailResponse(
        id=run.id,
        sprint_id=run.sprint_id,
        requirement_id=run.requirement_id,
        requirement_name=run.requirement_name,
        status=run.status,
        summary=run.summary,
        error=run.error,
        outdated_reasons=run.outdated_reasons,
        requirement_deleted=run.requirement_deleted,
        base_url_env_vars=run.base_url_env_vars,
        sessions=[_session_summary(s) for s in run.sessions],
        bug_count=bugs,
        issue_count=issues,
        high_severity_count=high,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _enqueue_run(session: Session, run: ExploratoryRun) -> None:
    """Best-effort enqueue after commit — failure is the reconciler's job."""
    job = get_queue_service().enqueue_exploration(run.id)
    if job is not None:
        run.job_id = job.id
        session.add(run)
        session.commit()
        session.refresh(run)


# ── charter drafting ──────────────────────────────────────────────────


@router.post(
    "/sprints/{sprint_id}/exploratory-charters/generate",
    response_model=ExploratoryCharterDraftResponse,
)
async def generate_charters(
    sprint_id: int,
    body: ExploratoryCharterGenerateRequest,
    session: Session = Depends(get_session),
) -> ExploratoryCharterDraftResponse:
    """Draft SBTM charters for one requirement. Persists nothing."""
    sprint = _get_sprint_or_404(session, sprint_id)
    _ensure_sprint_active(sprint)
    requirement = _resolve_requirement(sprint, body.requirement_id)
    env_vars = _resolve_env_vars(sprint)

    covered = [
        TestCaseLike(
            title=case.title,
            preconditions=case.preconditions,
            steps=case.steps,
            expected_result=case.expected_result,
            case_type=case.case_type,
            priority=case.priority,
        )
        for case in requirement.test_plan.cases
    ]
    readme = await resolve_readme(sprint)
    file_tree = sprint.repo.file_tree if sprint.repo else None

    try:
        result = await asyncio.to_thread(
            llm.generate_charters,
            name=requirement.name,
            description=requirement.description,
            covered_cases=covered,
            env_var_names=list(env_vars.keys()),
            readme=readme,
            file_tree=file_tree,
        )
    except llm.LLMError as exc:
        logger.warning("Sprint id=%d: charter generation failed: %s", sprint_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # The model only ever saw variable *names*; confirm its nominations
    # resolve to real http(s) URLs before the user is asked to approve them.
    _validate_url_vars(result.base_url_env_vars, env_vars, status_code=502)

    charters = [
        CharterDraft(charter=item.charter.strip(), sfdipot_areas=item.sfdipot_areas)
        for item in result.charters
    ]
    return ExploratoryCharterDraftResponse(
        requirement_id=requirement.id,
        requirement_name=requirement.name,
        charters=charters,
        base_url_env_vars=result.base_url_env_vars,
        charter_count=len(charters),
        projected_minutes=_projected_minutes(len(charters)),
    )


def _projected_minutes(charter_count: int) -> int:
    """Heuristic duration estimate, deliberately not asked of the model.

    The LLM has no idea how long a browser action or an LLM round takes, so
    any figure it returned would be invented — and an invented estimate is
    worse than none because it looks authoritative.

    Counts charged actions only, so it is a floor rather than a midpoint:
    ``record_finding`` is free of the action budget but still costs an LLM
    round, so a finding-heavy session overruns this. Left deliberately —
    sessions record a handful of findings at most, and quoting the
    theoretical worst case (every session hitting the finding cap) would
    overstate the common case far more than this understates it.
    """
    seconds = charter_count * EXPLORATORY_MAX_ACTIONS * EXPLORATORY_SECONDS_PER_ACTION
    return max(1, round(seconds / 60))


# ── runs ──────────────────────────────────────────────────────────────


@router.post(
    "/sprints/{sprint_id}/exploratory-runs",
    response_model=ExploratoryRunDetailResponse,
    status_code=201,
)
async def create_exploratory_run(
    sprint_id: int,
    body: ExploratoryRunCreateRequest,
    session: Session = Depends(get_session),
) -> ExploratoryRunDetailResponse:
    """Start an exploratory run over the approved charters for one requirement."""
    sprint = _get_sprint_or_404(session, sprint_id)
    _ensure_sprint_active(sprint)
    requirement = _resolve_requirement(sprint, body.requirement_id)
    env_vars = _resolve_env_vars(sprint)

    # The charters and URL variables come back edited, so nothing the generate
    # call returned is trusted here — both are re-validated from scratch.
    _validate_charters(body.charters)
    _validate_url_vars(body.base_url_env_vars, env_vars, status_code=422)

    if body.export_findings and sprint.issue_tracker is None:
        raise HTTPException(status_code=422, detail=TRACKER_REQUIRED_ERROR)

    if any(
        run.status in (ExploratoryRunStatus.PENDING, ExploratoryRunStatus.RUNNING)
        for run in requirement.exploratory_runs
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Requirement '{requirement.name}' already has an exploratory run in progress.",
        )

    # Refresh project context once for the whole run, best-effort — a user
    # -uploaded README is authoritative and is never overwritten.
    try:
        if not sprint.readme_user_provided:
            await resolve_readme(sprint, force_refresh=True)
        await refresh_file_tree(sprint)
        if sprint.repo is not None:
            session.add(sprint.repo)
            session.commit()
    except Exception as exc:
        logger.warning("Sprint id=%d: README/file tree refresh failed: %s", sprint_id, exc)

    run = ExploratoryRun(
        sprint_id=sprint_id,
        requirement_id=requirement.id,
        # Recorded so a later edit upstream marks this run outdated.
        requirement_revision=requirement.content_revision,
        plan_revision=requirement.test_plan.content_revision,
        env_revision=sprint.test_environment.content_revision,
        base_url_env_vars_csv=",".join(body.base_url_env_vars),
        export_findings=body.export_findings,
    )
    for position, charter in enumerate(body.charters):
        ExploratorySession(
            exploratory_run=run,
            position=position,
            charter=charter.charter.strip(),
            sfdipot_areas_csv=",".join(charter.sfdipot_areas),
        )

    session.add(run)
    session.commit()
    session.refresh(run)
    _enqueue_run(session, run)

    logger.info(
        "Sprint id=%d: exploratory run %d created with %d charter(s)",
        sprint_id,
        run.id,
        len(body.charters),
    )
    return _run_detail(_get_run_or_404(session, run.id))


@router.get("/sprints/{sprint_id}/exploratory-runs", response_model=list[ExploratoryRunResponse])
async def list_exploratory_runs(
    sprint_id: int,
    session: Session = Depends(get_session),
) -> list[ExploratoryRunResponse]:
    """List a sprint's exploratory runs, newest first."""
    _get_sprint_or_404(session, sprint_id)
    runs = session.exec(
        select(ExploratoryRun)
        .where(ExploratoryRun.sprint_id == sprint_id)
        .order_by(ExploratoryRun.created_at.desc(), ExploratoryRun.id.desc())
        .options(
            # Same reason as the scripted list: `outdated_reasons` walks
            # requirement → test_plan and sprint → test_environment, and this
            # endpoint polls every 2.5s.
            selectinload(ExploratoryRun.requirement).selectinload(Requirement.test_plan),
            selectinload(ExploratoryRun.sessions).selectinload(ExploratorySession.findings),
            selectinload(ExploratoryRun.sprint).selectinload(Sprint.test_environment),
        )
    ).all()
    return [_run_response(run) for run in runs]


@router.get("/exploratory-runs/{run_id}", response_model=ExploratoryRunDetailResponse)
async def get_exploratory_run(
    run_id: int,
    session: Session = Depends(get_session),
) -> ExploratoryRunDetailResponse:
    """Fetch one run — requirement, summary, finding roll-up, and its sessions."""
    return _run_detail(_get_run_or_404(session, run_id))


@router.get("/exploratory-sessions/{session_id}", response_model=ExploratorySessionResponse)
async def get_exploratory_session(
    session_id: int,
    session: Session = Depends(get_session),
) -> ExploratorySessionResponse:
    """Fetch one charter's full session sheet, including the action log."""
    row = session.get(ExploratorySession, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Exploratory session not found.")
    return ExploratorySessionResponse(
        id=row.id,
        exploratory_run_id=row.exploratory_run_id,
        position=row.position,
        charter=row.charter,
        sfdipot_areas=row.sfdipot_areas,
        status=row.status,
        actions_used=row.actions_used,
        session_notes=row.session_notes,
        action_log=row.action_log,
        stop_reason=row.stop_reason,
        error=row.error,
        findings=[_finding_response(f) for f in row.findings],
        updated_at=row.updated_at,
    )


@router.get("/exploratory-findings/{finding_id}/screenshot")
async def get_finding_screenshot(
    finding_id: int,
    session: Session = Depends(get_session),
) -> FileResponse:
    """Serve a finding's screenshot.

    404s when the finding carries none — the normal case when
    ``STORE_OFFLINE`` is disabled, which the UI renders as a finding without
    an image rather than a broken one.
    """
    finding = session.get(ExploratoryFinding, finding_id)
    if finding is None or not finding.screenshot_path:
        raise HTTPException(status_code=404, detail="No screenshot available for this finding.")
    if not os.path.isfile(finding.screenshot_path):
        raise HTTPException(status_code=404, detail="Screenshot file is no longer available.")
    return FileResponse(finding.screenshot_path, media_type="image/png")


@router.post("/exploratory-runs/{run_id}/restart", response_model=ExploratoryRunDetailResponse)
async def restart_exploratory_run(
    run_id: int,
    session: Session = Depends(get_session),
) -> ExploratoryRunDetailResponse:
    """Restart a failed run (uncapped, user-initiated).

    Charter-level resumability is derived entirely from each session's own
    status by the task — this route never touches the child rows.
    """
    run = _get_run_or_404(session, run_id)
    _ensure_sprint_active(run.sprint)

    if run.status != ExploratoryRunStatus.FAILED:
        raise HTTPException(
            status_code=422, detail="Only failed exploratory runs can be restarted."
        )
    # A plan edit does not stop a session already under way (see
    # tasks/explore_requirement.py) and must not block a restart either —
    # a charter's cases were consumed once, at generation time. The run is
    # still *badged* for it; this only decides whether it can run again.
    blocking = [reason for reason in run.outdated_reasons if reason != "test_plan"]
    if blocking:
        raise HTTPException(
            status_code=422,
            detail=outdated_restart_error(blocking, run.requirement_deleted),
        )

    run.status = ExploratoryRunStatus.PENDING
    run.error = None
    run.retry_count = 0
    run.updated_at = datetime.now(timezone.utc)
    session.add(run)
    session.commit()
    session.refresh(run)
    _enqueue_run(session, run)
    return _run_detail(_get_run_or_404(session, run_id))


@router.post("/exploratory-runs/{run_id}/summarize", response_model=ExploratoryRunDetailResponse)
async def summarize_exploratory_run(
    run_id: int,
    session: Session = Depends(get_session),
) -> ExploratoryRunDetailResponse:
    """Retry the best-effort summary the task may have left null.

    Synchronous, like charter generation — one cheap completion, no queue.
    Regenerates rather than only filling nulls: allowing it on a run that
    already has a summary costs one call and removes a state to reason about.
    """
    run = _get_run_or_404(session, run_id)
    if run.status != ExploratoryRunStatus.COMPLETED:
        raise HTTPException(
            status_code=422,
            detail="Only completed exploratory runs can be summarized.",
        )

    requirement = run.requirement
    if requirement is None:
        raise HTTPException(status_code=422, detail="This run's requirement no longer exists.")

    try:
        result = await asyncio.to_thread(
            llm.summarize_exploration,
            name=requirement.name,
            description=requirement.description,
            sessions=session_sheets(run),
        )
    except llm.LLMError as exc:
        logger.warning("Exploratory run %d: summary retry failed: %s", run_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    run.summary = result.summary
    run.updated_at = datetime.now(timezone.utc)
    session.add(run)
    session.commit()
    session.refresh(run)
    return _run_detail(_get_run_or_404(session, run_id))
