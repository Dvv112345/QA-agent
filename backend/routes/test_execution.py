"""Test-execution routes — create runs, list/detail, script download, restart."""

import asyncio
import logging
from datetime import datetime, timezone
from functools import partial

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from backend.database import get_session
from backend.models.database import (
    Requirement,
    RequirementStatus,
    Sprint,
    TestCaseExecution,
    TestCaseExecutionStatus,
    TestEnvironmentStatus,
    TestExecution,
    TestExecutionStatus,
    TestPlanStatus,
    TestRun,
    export_rollup,
    outdated_restart_error,
)
from backend.models.types import (
    TestExecutionResponse,
    TestRunCreateRequest,
    TestRunDetailResponse,
    TestRunResponse,
)
from backend.routes._common import ensure_sprint_active, get_sprint_or_404
from backend.services import finding_export
from backend.services.finding_export import TRACKER_REQUIRED_ERROR
from backend.services.queue import enqueue_rows, get_queue_service
from backend.utils.auth import verify_auth
from backend.utils.readme_utils import refresh_project_context

logger = logging.getLogger(__name__)

# Completes "Sprint is finished — {}." for every gate in this module.
_GATE_SUBJECT = "test runs can no longer be created or restarted"

router = APIRouter(dependencies=[Depends(verify_auth)])


def _get_run_or_404(session: Session, run_id: int) -> TestRun:
    run = session.exec(
        select(TestRun)
        .where(TestRun.id == run_id)
        .options(
            selectinload(TestRun.executions)
            .selectinload(TestExecution.requirement)
            .selectinload(Requirement.test_plan),
            selectinload(TestRun.executions)
            .selectinload(TestExecution.cases)
            .selectinload(TestCaseExecution.test_case),
            selectinload(TestRun.sprint).selectinload(Sprint.test_environment),
        )
    ).one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Test run not found.")
    return run


def _get_execution_or_404(session: Session, execution_id: int) -> TestExecution:
    execution = session.get(TestExecution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Test execution not found.")
    return execution


def _run_response(run: TestRun) -> TestRunResponse:
    """Build the list-page shape — case-count aggregates are response-only,
    not stored on TestRun (Convention #10 spirit, applied one level down)."""
    total = passed = failed = error = 0
    for execution in run.executions:
        for case in execution.cases:
            total += 1
            if case.status == TestCaseExecutionStatus.PASSED:
                passed += 1
            elif case.status == TestCaseExecutionStatus.FAILED:
                failed += 1
            elif case.status == TestCaseExecutionStatus.ERROR:
                error += 1
    return TestRunResponse(
        id=run.id,
        sprint_id=run.sprint_id,
        created_at=run.created_at,
        status=run.status,
        outdated_reasons=run.outdated_reasons,
        requirement_deleted=run.requirement_deleted,
        requirement_names=run.requirement_names,
        total_cases=total,
        passed_cases=passed,
        failed_cases=failed,
        error_cases=error,
        **export_rollup(run.bug_findings, export_findings=run.export_findings),
    )


def _run_detail(run: TestRun) -> TestRunDetailResponse:
    """Build the detail shape, computing the export roll-up exactly once.

    The twin of ``routes/exploratory.py::_run_detail``.  `executions` is
    passed through as rows for SQLModel's ``from_attributes`` to coerce,
    which is what FastAPI did with the whole object before this existed —
    only the roll-up needs composing by hand.
    """
    return TestRunDetailResponse(
        id=run.id,
        sprint_id=run.sprint_id,
        created_at=run.created_at,
        status=run.status,
        outdated_reasons=run.outdated_reasons,
        requirement_deleted=run.requirement_deleted,
        executions=run.executions,
        **export_rollup(run.bug_findings, export_findings=run.export_findings),
    )


@router.post(
    "/sprints/{sprint_id}/test-runs", response_model=TestRunDetailResponse, status_code=201
)
async def create_test_run(
    sprint_id: int,
    body: TestRunCreateRequest,
    session: Session = Depends(get_session),
) -> TestRunDetailResponse:
    """Create a run covering the selected requirements — one TestExecution
    (+ TestCaseExecution per approved-plan case) each, enqueued best-effort."""
    sprint = get_sprint_or_404(session, sprint_id)
    ensure_sprint_active(sprint, _GATE_SUBJECT)

    if not body.requirement_ids:
        raise HTTPException(status_code=422, detail="At least one requirement must be selected.")

    if body.export_findings and sprint.issue_tracker is None:
        raise HTTPException(status_code=422, detail=TRACKER_REQUIRED_ERROR)

    # Scripted runs need this as much as exploratory ones do — the worker
    # injects `env_vars` into every script's subprocess. It went unchecked
    # while a confirmed environment was permanent; now that adding a
    # requirement un-confirms it without removing plans, a run can be started
    # against an environment that is no longer confirmed, and every case
    # fails on missing variables. Mirrors `_resolve_env_vars` in
    # routes/exploratory.py.
    test_env = sprint.test_environment
    if test_env is None or test_env.status != TestEnvironmentStatus.CONFIRMED:
        raise HTTPException(
            status_code=422,
            detail="Confirm the test environment before running tests.",
        )
    if not test_env.env_vars:
        raise HTTPException(
            status_code=422,
            detail="No test environment variables are available for this sprint.",
        )

    requirements_by_id = {r.id: r for r in sprint.requirements}
    selected = []
    invalid_ids = []
    for req_id in body.requirement_ids:
        requirement = requirements_by_id.get(req_id)
        if requirement is None or requirement.status != RequirementStatus.CONFIRMED:
            invalid_ids.append(req_id)
        else:
            selected.append(requirement)
    if invalid_ids:
        raise HTTPException(
            status_code=422,
            detail=(f"Requirement id(s) not found or not confirmed in this sprint: {invalid_ids}."),
        )

    not_approved = [
        r.name
        for r in selected
        if r.test_plan is None or r.test_plan.status != TestPlanStatus.APPROVED
    ]
    if not_approved:
        raise HTTPException(
            status_code=422,
            detail=(
                f"These requirements do not have an approved test plan: {', '.join(not_approved)}."
            ),
        )

    in_progress = [
        r.name
        for r in selected
        if any(
            e.status in (TestExecutionStatus.PENDING, TestExecutionStatus.RUNNING)
            for e in r.test_executions
        )
    ]
    if in_progress:
        raise HTTPException(
            status_code=422,
            detail=f"These requirements already have a run in progress: {', '.join(in_progress)}.",
        )

    # ── Refresh README/file tree once for the whole run (best-effort) ──
    # Every TestExecution below is a separate RQ job/process, so this is
    # the one synchronous choke point they all share — refresh here rather
    # than per execution or per case.
    await refresh_project_context(session, sprint)

    # Each execution records the content revisions it is about to run
    # against; a later edit upstream is what makes it read as outdated.
    env_revision = test_env.content_revision
    run = TestRun(sprint_id=sprint_id, export_findings=body.export_findings)
    executions: list[TestExecution] = []
    for requirement in selected:
        execution = TestExecution(
            test_run=run,
            requirement_id=requirement.id,
            requirement_revision=requirement.content_revision,
            plan_revision=requirement.test_plan.content_revision,
            env_revision=env_revision,
        )
        for case in requirement.test_plan.cases:
            TestCaseExecution(test_execution=execution, test_case_id=case.id)
        executions.append(execution)

    session.add(run)
    session.commit()
    for execution in executions:
        session.refresh(execution)

    enqueue_rows(session, executions, get_queue_service().enqueue_test_execution)

    logger.info(
        "Sprint id=%d: test run %d created covering %d requirement(s)",
        sprint_id,
        run.id,
        len(executions),
    )
    return _run_detail(_get_run_or_404(session, run.id))


@router.get("/sprints/{sprint_id}/test-runs", response_model=list[TestRunResponse])
async def list_test_runs(
    sprint_id: int,
    session: Session = Depends(get_session),
) -> list[TestRunResponse]:
    """List a sprint's test runs, newest first (Decision 11)."""
    get_sprint_or_404(session, sprint_id)
    runs = session.exec(
        select(TestRun)
        .where(TestRun.sprint_id == sprint_id)
        .order_by(TestRun.created_at.desc(), TestRun.id.desc())
        .options(
            # `outdated_reasons` walks requirement → test_plan and
            # run → sprint → test_environment. This endpoint is polled every
            # 2.5s, so leaving those to lazy loads costs one query per
            # distinct requirement on every tick.
            selectinload(TestRun.executions)
            .selectinload(TestExecution.requirement)
            .selectinload(Requirement.test_plan),
            selectinload(TestRun.executions).selectinload(TestExecution.cases),
            selectinload(TestRun.sprint).selectinload(Sprint.test_environment),
        )
    ).all()
    return [_run_response(run) for run in runs]


@router.get("/test-runs/{run_id}", response_model=TestRunDetailResponse)
async def get_test_run(
    run_id: int,
    session: Session = Depends(get_session),
) -> TestRunDetailResponse:
    """Fetch one run's full detail — grouped by requirement, then by case."""
    return _run_detail(_get_run_or_404(session, run_id))


@router.get("/test-case-executions/{case_execution_id}/script")
async def download_test_case_script(
    case_execution_id: int,
    session: Session = Depends(get_session),
) -> PlainTextResponse:
    """Download the exact script that produced this case's result (credential-free)."""
    case_execution = session.get(TestCaseExecution, case_execution_id)
    if case_execution is None or case_execution.script_snapshot is None:
        raise HTTPException(
            status_code=404, detail="No script available for this test case execution."
        )
    return PlainTextResponse(
        case_execution.script_snapshot,
        headers={"Content-Disposition": f'attachment; filename="test_case_{case_execution_id}.py"'},
    )


@router.post("/test-executions/{execution_id}/restart", response_model=TestExecutionResponse)
async def restart_test_execution(
    execution_id: int,
    session: Session = Depends(get_session),
) -> TestExecution:
    """Restart a failed execution (uncapped, user-initiated).

    Case-level resumability is derived entirely from each
    ``TestCaseExecution``'s own status by the task — this route never
    touches the child rows.
    """
    execution = _get_execution_or_404(session, execution_id)
    ensure_sprint_active(
        execution.requirement.sprint if execution.requirement else None, _GATE_SUBJECT
    )

    if execution.status != TestExecutionStatus.FAILED:
        raise HTTPException(status_code=422, detail="Only failed test executions can be restarted.")
    if execution.outdated:
        raise HTTPException(
            status_code=422,
            detail=outdated_restart_error(
                execution.outdated_reasons, execution.requirement_deleted
            ),
        )

    execution.status = TestExecutionStatus.PENDING
    execution.error = None
    execution.retry_count = 0
    execution.updated_at = datetime.now(timezone.utc)
    session.add(execution)
    session.commit()
    session.refresh(execution)
    enqueue_rows(session, [execution], get_queue_service().enqueue_test_execution)
    return execution


@router.post("/test-runs/{run_id}/export-findings", response_model=TestRunDetailResponse)
async def export_test_run_findings(
    run_id: int,
    session: Session = Depends(get_session),
) -> TestRunDetailResponse:
    """File this run's unfiled bug findings, on request.

    The manual half of the export rule, and not a fallback: a run that
    ended any way other than ``completed`` reaches its page with the bugs
    it *did* find unfiled by design, and this is how they get filed.
    Retrying a run whose filing failed is the same operation.

    Synchronous via ``asyncio.to_thread`` — like
    ``POST /exploratory-runs/{id}/summarize``, this is user-initiated,
    uncapped, and cheap enough that a queue would only add a place for it
    to get lost.  A run with nothing pending is a no-op that still
    returns the refreshed detail, so the button is never a trap.
    """
    run = _get_run_or_404(session, run_id)
    if run.sprint is not None and run.sprint.issue_tracker is None:
        raise HTTPException(status_code=422, detail=TRACKER_REQUIRED_ERROR)

    # `requested=True`: the click is the consent the run's toggle stands in
    # for, so this files even for a run started before a tracker existed.
    for execution in run.executions:
        await asyncio.to_thread(
            partial(finding_export.export_findings, session, execution, requested=True)
        )

    session.expire_all()
    return _run_detail(_get_run_or_404(session, run_id))
