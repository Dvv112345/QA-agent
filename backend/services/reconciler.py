"""Reconciler — re-enqueues background jobs lost to Redis or worker crashes.

Runs as an asyncio background task started by the FastAPI lifespan.  Each
tick (``reconcile_once``) does four things, for every job-backed row type
(requirements and test plans — see ``SWEEP_SPECS``):

1. If Redis was down, rebuild the queue-service singleton (reconnect).
2. Fail pending/running rows whose sprint is inactive — races around
   sprint finish can recreate them after ``finish_sprint``'s own sweep,
   and nothing may stay in-progress on a finished sprint.
3. Sweep running rows whose worker heartbeat went stale (crashed worker)
   back to pending — or to failed once auto-retries are exhausted.
4. Enqueue every pending row that has no live RQ job — including rows
   whose job *did* start but crashed before the task's first commit
   flipped it to running (detected via a stale RQ ``job.started_at``),
   which get the same retry/fail disposition as (3).

The database sweeps (2–3) run even while Redis is down; only the enqueue
sweep (4) needs the queue.

Every branch above that lands a row on ``failed`` also settles that row's
child rows (``services/finalization.py``) — a failed test execution or
exploratory run must not leave its cases or charter sessions reading as
"Queued" forever.  The branches that send a row back to ``pending`` must
*not*, since the next attempt resumes exactly where the last one stopped.

PostgreSQL is the status of record, so a tick is idempotent and safe to run
concurrently with user actions: the tasks' own status guards skip rows the
user confirmed, approved, or edited meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlmodel import select
from sqlmodel.sql.expression import SelectOfScalar

from backend.config import (
    HEARTBEAT_STALE_SECONDS,
    MAX_AUTO_RETRIES,
    PENDING_JOB_STALE_SECONDS,
    RECONCILER_INTERVAL,
)
from backend.database import new_session
from backend.models.database import (
    SPRINT_FINISHED_ERROR,
    CicdExport,
    CicdExportStatus,
    ExploratoryRun,
    ExploratoryRunStatus,
    NonfunctionalRun,
    NonfunctionalRunStatus,
    Requirement,
    RequirementStatus,
    Sprint,
    TestExecution,
    TestExecutionStatus,
    TestPlan,
    TestPlanStatus,
)
from backend.services.finalization import (
    EXPLORATORY_SESSION_SPEC,
    LOAD_PROFILE_SPEC,
    NONFUNCTIONAL_TARGET_SPEC,
    TEST_CASE_SPEC,
    ChildSpec,
    abandon_unreached_children,
)
from backend.services.queue import get_queue_service, reset_queue_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepSpec:
    """One job-backed row type the reconciler sweeps.

    The machinery columns (``status``/``retry_count``/``job_id``/
    ``last_heartbeat``/``error``/``updated_at``) share names across models,
    which is what lets the per-row disposition code below be common — only
    the query scoping, the "pending user input" field, and the enqueue
    method differ.
    """

    model: type
    label: str
    pending_status: str
    running_status: str
    failed_status: str
    # Pending user input, cleared when the row is failed. None for row
    # types with no such field (e.g. TestExecution) — the setattr is
    # skipped rather than clobbering an unrelated column.
    clear_field: str | None
    enqueue_name: str  # QueueService method used to (re-)enqueue
    stale_error: str  # user-facing error once retries are exhausted
    # Child row types to settle whenever this row type is *failed* here, so
    # a terminal parent never leaves cases or charter sessions reading as
    # "Queued" forever. Empty for Requirement/TestPlan, which have none; a
    # tuple because one run type may drive more than one kind of child row.
    #
    # Deliberately not applied on the re-pend branches: a `running` child
    # belonging to a row going back to `pending` must stay as it is, since
    # the next attempt resumes exactly there.
    child_specs: tuple[ChildSpec, ...]
    # Joins the row type to Sprint so sprint activity can be filtered on.
    #
    # Deliberately does *not* exclude rows belonging to an archived
    # requirement. That filter was here and caused a row on a deleted
    # requirement to be swept by nothing: never re-enqueued, so never picked
    # up by the task that would fail it, and never failed by these sweeps
    # either — leaving a `pending` execution forever and a run page polling
    # `running` with nothing behind it. The tasks already refuse an archived
    # requirement before spending any LLM call, so enqueuing one costs a
    # no-op job and converges, which is the cheaper mistake.
    scope_query: Callable[[SelectOfScalar], SelectOfScalar]
    # Whether this row type may run on a *finished* sprint. True only for
    # CicdExport: exporting verified scripts to CI is exactly what a team
    # wants once the testing is done.
    #
    # Read at **three** sites, not two. The two inactive-sprint sweeps skip
    # such a spec, and `_sweep_pending` additionally drops its `Sprint.active`
    # predicate for it — without that third one a pending export on a
    # finished sprint is invisible to every sweep: the inactive sweeps skip
    # it, the heartbeat sweep only sees `running` rows, and the pending sweep
    # filters it out. After a Redis outage it would sit `pending` forever,
    # with Restart re-pending it into the same hole.
    #
    # Defaulting to False means the four existing specs need no edit and the
    # one exception declares itself.
    inactive_sprint_ok: bool = False


SWEEP_SPECS: tuple[SweepSpec, ...] = (
    SweepSpec(
        model=Requirement,
        label="Requirement",
        pending_status=RequirementStatus.PENDING,
        running_status=RequirementStatus.ANALYZING,
        failed_status=RequirementStatus.FAILED,
        clear_field="pending_answer",
        enqueue_name="enqueue_analysis",
        stale_error=(
            "Analysis worker died repeatedly while processing this requirement. "
            "Use Restart to try again."
        ),
        child_specs=(),
        scope_query=lambda stmt: stmt.join(Sprint),
    ),
    SweepSpec(
        model=TestPlan,
        label="Test plan",
        pending_status=TestPlanStatus.PENDING,
        running_status=TestPlanStatus.GENERATING,
        failed_status=TestPlanStatus.FAILED,
        clear_field="pending_feedback",
        enqueue_name="enqueue_test_plan",
        stale_error=(
            "Generation worker died repeatedly while processing this test plan. "
            "Use Restart to try again."
        ),
        child_specs=(),
        scope_query=lambda stmt: stmt.join(
            Requirement, TestPlan.requirement_id == Requirement.id
        ).join(Sprint, Requirement.sprint_id == Sprint.id),
    ),
    SweepSpec(
        model=TestExecution,
        label="Test execution",
        pending_status=TestExecutionStatus.PENDING,
        running_status=TestExecutionStatus.RUNNING,
        failed_status=TestExecutionStatus.FAILED,
        clear_field=None,
        enqueue_name="enqueue_test_execution",
        stale_error=(
            "Execution worker died repeatedly while processing this test run. "
            "Use Restart to try again."
        ),
        child_specs=(TEST_CASE_SPEC,),
        scope_query=lambda stmt: stmt.join(
            Requirement, TestExecution.requirement_id == Requirement.id
        ).join(Sprint, Requirement.sprint_id == Sprint.id),
    ),
    SweepSpec(
        model=ExploratoryRun,
        label="Exploratory run",
        pending_status=ExploratoryRunStatus.PENDING,
        running_status=ExploratoryRunStatus.RUNNING,
        failed_status=ExploratoryRunStatus.FAILED,
        clear_field=None,
        enqueue_name="enqueue_exploration",
        stale_error=(
            "Exploration worker died repeatedly while processing this run. "
            "Use Restart to try again."
        ),
        child_specs=(EXPLORATORY_SESSION_SPEC,),
        # Joins straight to Sprint — unlike plans and executions, an
        # exploratory run carries its own sprint_id.
        scope_query=lambda stmt: stmt.join(Sprint, ExploratoryRun.sprint_id == Sprint.id),
    ),
    SweepSpec(
        model=NonfunctionalRun,
        label="Nonfunctional run",
        pending_status=NonfunctionalRunStatus.PENDING,
        running_status=NonfunctionalRunStatus.RUNNING,
        failed_status=NonfunctionalRunStatus.FAILED,
        clear_field=None,
        enqueue_name="enqueue_nonfunctional_run",
        stale_error=(
            "Nonfunctional worker died repeatedly while processing this run. "
            "Use Restart to try again."
        ),
        # Both child types — the URLs it examined and the traffic it
        # applied. See NONFUNCTIONAL_RUN_SPEC for why a settled load
        # profile still must not be re-sent.
        child_specs=(NONFUNCTIONAL_TARGET_SPEC, LOAD_PROFILE_SPEC),
        # Carries its own sprint_id, like ExploratoryRun.
        scope_query=lambda stmt: stmt.join(Sprint, NonfunctionalRun.sprint_id == Sprint.id),
    ),
    SweepSpec(
        model=CicdExport,
        label="CI/CD export",
        pending_status=CicdExportStatus.PENDING,
        running_status=CicdExportStatus.RUNNING,
        failed_status=CicdExportStatus.FAILED,
        clear_field=None,
        enqueue_name="enqueue_cicd_export",
        stale_error=(
            "Export worker died repeatedly while processing this export. Use Restart to try again."
        ),
        child_specs=(),
        # Carries its own sprint_id, like ExploratoryRun.
        scope_query=lambda stmt: stmt.join(Sprint, CicdExport.sprint_id == Sprint.id),
        inactive_sprint_ok=True,
    ),
)


def _is_stale(timestamp: datetime | None, now: datetime, threshold_seconds: int) -> bool:
    """Whether a timestamp is older than ``threshold_seconds``.

    Used for both an ``analyzing`` row's worker heartbeat and a ``pending``
    row's RQ ``job.started_at``. SQLite (tests) and timestamp-without-timezone
    columns return naive datetimes — normalise to aware UTC before comparing.
    """
    if timestamp is None:
        return True
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (now - timestamp).total_seconds() > threshold_seconds


def _settle_children(session, spec: SweepSpec, parent_id: int, reason: str) -> None:
    """Settle a just-failed row's child rows, if its type has any.

    A no-op for Requirement/TestPlan.  Called only from the branches that
    reach ``failed_status`` — never from the ones that re-pend a row, which
    are resumed exactly where they stopped.
    """
    for child_spec in spec.child_specs:
        abandon_unreached_children(session, child_spec, parent_id, reason)


def fail_in_progress_rows(session, spec: SweepSpec, scope, now: datetime) -> list:
    """Fail every in-progress row of one type matching ``scope``.

    The shared body of the two sweeps that retire work on a sprint nobody
    can act on any more: the reconciler's convergence pass (``scope`` =
    every inactive sprint) and ``routes/sprints.finish_sprint`` (``scope``
    = this one sprint).  ``spec.scope_query`` supplies the join to
    ``Sprint`` that lets both express their filter as a plain predicate.

    Stages without committing, so the caller decides the transaction —
    ``finish_sprint`` lands this in the same commit that deactivates the
    sprint.  Returns the affected rows; each caller logs its own way, one
    per row for the reconciler and one aggregate per type for the route.
    """
    rows = session.exec(
        spec.scope_query(select(spec.model)).where(
            spec.model.status.in_([spec.pending_status, spec.running_status]),
            scope,
        )
    ).all()
    for row in rows:
        row.status = spec.failed_status
        row.error = SPRINT_FINISHED_ERROR
        row.last_heartbeat = None
        if spec.clear_field is not None:
            setattr(row, spec.clear_field, None)
        row.updated_at = now
        session.add(row)
        # A terminal parent leaves no non-terminal children — otherwise the
        # cases or charters this run never reached read as "Queued" forever
        # on a sprint that can no longer run anything.
        _settle_children(session, spec, row.id, SPRINT_FINISHED_ERROR)
    return list(rows)


def _sweep_inactive_sprints(session, spec: SweepSpec, now: datetime) -> None:
    """Fail in-progress rows on finished sprints.

    finish_sprint fails in-progress rows in its own commit, but races
    around the finish can recreate them (a task failure re-pending a row,
    a running row the finish sweep missed).  Converge them to the same
    failed state so nothing stays in-progress forever on a finished
    sprint.  Runs before the stale-heartbeat sweep so such rows are
    failed, not re-pended.

    Skipped entirely for a spec that may run on a finished sprint — see
    ``SweepSpec.inactive_sprint_ok``.
    """
    if spec.inactive_sprint_ok:
        return
    orphaned = fail_in_progress_rows(
        session,
        spec,
        Sprint.active.is_(False),  # type: ignore[attr-defined]
        now,
    )
    for row in orphaned:
        logger.info("%s %d: sprint inactive — marked failed", spec.label, row.id)


def _sweep_stale_heartbeats(session, spec: SweepSpec, now: datetime) -> None:
    """Return running rows with a stale worker heartbeat to pending (or fail)."""
    running = session.exec(select(spec.model).where(spec.model.status == spec.running_status)).all()
    for row in running:
        if not _is_stale(row.last_heartbeat, now, HEARTBEAT_STALE_SECONDS):
            continue
        row.retry_count += 1
        if row.retry_count >= MAX_AUTO_RETRIES:
            row.status = spec.failed_status
            row.error = spec.stale_error
            _settle_children(session, spec, row.id, spec.stale_error)
            logger.warning(
                "%s %d: worker heartbeat stale, retries exhausted → failed", spec.label, row.id
            )
        else:
            row.status = spec.pending_status
            logger.info(
                "%s %d: worker heartbeat stale → pending (retry %d)",
                spec.label,
                row.id,
                row.retry_count,
            )
        row.last_heartbeat = None
        # Clear job_id: the old RQ job is permanently stuck at "started"
        # since that worker died, and the pending sweep's staleness check
        # would otherwise re-detect it and double-count this same crash.
        row.job_id = None
        row.updated_at = now
        session.add(row)


def _sweep_pending(session, spec: SweepSpec, queue_service: Any, now: datetime) -> None:
    """Enqueue pending rows without a live RQ job.

    Finished sprints are excluded — their rows were failed by the
    inactive-sprint sweep — **except** for a spec marked
    ``inactive_sprint_ok``, whose rows are legitimate there and which that
    sweep therefore skipped.  Dropping the predicate is what keeps such a
    row recoverable after a Redis outage; leaving it in makes the row
    invisible to all three sweeps at once.
    """
    enqueue: Callable[[int], Any] = getattr(queue_service, spec.enqueue_name)
    stmt = spec.scope_query(select(spec.model)).where(spec.model.status == spec.pending_status)
    if not spec.inactive_sprint_ok:
        stmt = stmt.where(Sprint.active)
    pending = session.exec(stmt).all()
    for row in pending:
        if row.job_id:
            existing_job = queue_service.get_job(row.job_id)
            if existing_job is not None:
                job_status = existing_job.get_status()
                if job_status == "queued":
                    continue  # waiting normally — dedup, no action
                if job_status == "started":
                    if not _is_stale(existing_job.started_at, now, PENDING_JOB_STALE_SECONDS):
                        continue  # actively being worked on — dedup, no action
                    # Worker crashed before flipping the row to running.
                    row.retry_count += 1
                    row.job_id = None
                    row.updated_at = now
                    if row.retry_count >= MAX_AUTO_RETRIES:
                        row.status = spec.failed_status
                        row.error = spec.stale_error
                        session.add(row)
                        _settle_children(session, spec, row.id, spec.stale_error)
                        logger.warning(
                            "%s %d: worker crashed before starting, retries exhausted → failed",
                            spec.label,
                            row.id,
                        )
                        continue  # terminal — skip enqueue below
                    logger.info(
                        "%s %d: worker crashed before starting → retry %d",
                        spec.label,
                        row.id,
                        row.retry_count,
                    )
                    session.add(row)
        new_job = enqueue(row.id)
        if new_job is not None:
            row.job_id = new_job.id
            row.updated_at = now
            session.add(row)
            logger.info(
                "Reconciler enqueued %s %d as job %s", spec.label.lower(), row.id, new_job.id
            )


def reconcile_once() -> None:
    """Run one reconciliation tick (synchronous; called via ``asyncio.to_thread``)."""
    queue_service = get_queue_service()
    if not queue_service.available:
        # Redis may have recovered since the singleton last tried to connect.
        reset_queue_service()
        queue_service = get_queue_service()
        if not queue_service.available:
            # The database sweeps below still run — only enqueueing needs Redis.
            logger.debug("Reconciler: Redis unavailable — skipping the enqueue sweep")

    now = datetime.now(timezone.utc)
    with new_session() as session:
        for spec in SWEEP_SPECS:
            _sweep_inactive_sprints(session, spec, now)
            _sweep_stale_heartbeats(session, spec, now)
            if queue_service.available:
                _sweep_pending(session, spec, queue_service, now)

        session.commit()


async def reconciler_loop() -> None:
    """Run ``reconcile_once`` forever; a failing tick never kills the loop."""
    logger.info("Reconciler started (interval %ds)", RECONCILER_INTERVAL)
    while True:
        try:
            await asyncio.to_thread(reconcile_once)
        except Exception:
            logger.exception("Reconciler tick failed")
        await asyncio.sleep(RECONCILER_INTERVAL)
