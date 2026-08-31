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
from functools import partial

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
    SfdipotArea,
    Sprint,
    export_rollup,
    outdated_restart_error,
)
from backend.models.types import (
    CharterDraft,
    ExploratoryCharterDraftResponse,
    ExploratoryCharterGenerateRequest,
    ExploratoryRunCreateRequest,
    ExploratoryRunDetailResponse,
    ExploratoryRunResponse,
    ExploratorySessionResponse,
)
from backend.routes._common import (
    ensure_sprint_active,
    get_sprint_or_404,
    resolve_confirmed_env_vars,
    resolve_requirement_for_run,
    validate_url_vars,
)
from backend.services import finding_export, llm
from backend.services.finding_export import TRACKER_REQUIRED_ERROR
from backend.services.llm_prompts import TestCaseLike
from backend.services.queue import enqueue_rows, get_queue_service
from backend.utils.auth import verify_auth
from backend.utils.exploratory_utils import session_sheets
from backend.utils.readme_utils import refresh_project_context, resolve_readme

logger = logging.getLogger(__name__)

# Completes "Sprint is finished — {}." for every gate in this module.
_GATE_SUBJECT = "exploratory runs can no longer be created"

router = APIRouter(dependencies=[Depends(verify_auth)])


# ── lookups and guards ────────────────────────────────────────────────


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
        **export_rollup(run.bug_findings, export_findings=run.export_findings),
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
        session_count=len(run.sessions),
        base_url_env_vars=run.base_url_env_vars,
        sessions=run.sessions,
        bug_count=bugs,
        issue_count=issues,
        high_severity_count=high,
        **export_rollup(run.bug_findings, export_findings=run.export_findings),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


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
    sprint = get_sprint_or_404(session, sprint_id)
    ensure_sprint_active(sprint, _GATE_SUBJECT)
    requirement = resolve_requirement_for_run(sprint, body.requirement_id)
    env_vars = resolve_confirmed_env_vars(sprint)

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
    validate_url_vars(result.base_url_env_vars, env_vars, status_code=502)

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
    sprint = get_sprint_or_404(session, sprint_id)
    ensure_sprint_active(sprint, _GATE_SUBJECT)
    requirement = resolve_requirement_for_run(sprint, body.requirement_id)
    env_vars = resolve_confirmed_env_vars(sprint)

    # The charters and URL variables come back edited, so nothing the generate
    # call returned is trusted here — both are re-validated from scratch.
    _validate_charters(body.charters)
    validate_url_vars(body.base_url_env_vars, env_vars, status_code=422)

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

    # Refresh project context once for the whole run.
    await refresh_project_context(session, sprint)

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
    enqueue_rows(session, [run], get_queue_service().enqueue_exploration)

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
    get_sprint_or_404(session, sprint_id)
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
    return row


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
    ensure_sprint_active(run.sprint, _GATE_SUBJECT)

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
    # No refresh here: `enqueue_rows` commits and refreshes the row itself,
    # and the reload below re-reads it with its relationships eager-loaded.
    enqueue_rows(session, [run], get_queue_service().enqueue_exploration)
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


@router.post(
    "/exploratory-runs/{run_id}/export-findings",
    response_model=ExploratoryRunDetailResponse,
)
async def export_exploratory_run_findings(
    run_id: int,
    session: Session = Depends(get_session),
) -> ExploratoryRunDetailResponse:
    """File this run's unfiled bug findings, on request.

    The exploratory twin of ``POST /test-runs/{id}/export-findings`` —
    see it for why this is the manual half of the export rule rather than
    a fallback.
    """
    run = _get_run_or_404(session, run_id)
    if run.sprint is not None and run.sprint.issue_tracker is None:
        raise HTTPException(status_code=422, detail=TRACKER_REQUIRED_ERROR)

    # See the scripted twin: `requested=True` because the click is itself
    # the consent the run's toggle stands in for.
    await asyncio.to_thread(partial(finding_export.export_findings, session, run, requested=True))

    session.expire_all()
    return _run_detail(_get_run_or_404(session, run_id))
