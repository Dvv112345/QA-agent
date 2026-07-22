"""Reconciler — re-enqueues background jobs lost to Redis or worker crashes.

Runs as an asyncio background task started by the FastAPI lifespan.  Each
tick (``reconcile_once``) does four things, for every job-backed row type
(requirements and test plans — see ``_SWEEP_SPECS``):

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
    Requirement,
    RequirementStatus,
    Sprint,
    TestExecution,
    TestExecutionStatus,
    TestPlan,
    TestPlanStatus,
)
from backend.services.queue import get_queue_service, reset_queue_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SweepSpec:
    """One job-backed row type the reconciler sweeps.

    The machinery columns (``status``/``retry_count``/``job_id``/
    ``last_heartbeat``/``error``/``updated_at``) share names across models,
    which is what lets the per-row disposition code below be common — only
    the selects (sprint join), the "pending user input" field, and the
    enqueue method differ.
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
    join_to_sprint: Callable[[SelectOfScalar], SelectOfScalar]


_SWEEP_SPECS: tuple[_SweepSpec, ...] = (
    _SweepSpec(
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
        join_to_sprint=lambda stmt: stmt.join(Sprint),
    ),
    _SweepSpec(
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
        join_to_sprint=lambda stmt: stmt.join(
            Requirement, TestPlan.requirement_id == Requirement.id
        ).join(Sprint, Requirement.sprint_id == Sprint.id),
    ),
    _SweepSpec(
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
        join_to_sprint=lambda stmt: stmt.join(
            Requirement, TestExecution.requirement_id == Requirement.id
        ).join(Sprint, Requirement.sprint_id == Sprint.id),
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


def _sweep_inactive_sprints(session, spec: _SweepSpec, now: datetime) -> None:
    """Fail in-progress rows on finished sprints.

    finish_sprint fails in-progress rows in its own commit, but races
    around the finish can recreate them (a task failure re-pending a row,
    a running row the finish sweep missed).  Converge them to the same
    failed state so nothing stays in-progress forever on a finished
    sprint.  Runs before the stale-heartbeat sweep so such rows are
    failed, not re-pended.
    """
    orphaned = session.exec(
        spec.join_to_sprint(select(spec.model)).where(
            spec.model.status.in_([spec.pending_status, spec.running_status]),
            Sprint.active.is_(False),  # type: ignore[attr-defined]
        )
    ).all()
    for row in orphaned:
        row.status = spec.failed_status
        row.error = SPRINT_FINISHED_ERROR
        row.last_heartbeat = None
        if spec.clear_field is not None:
            setattr(row, spec.clear_field, None)
        row.updated_at = now
        session.add(row)
        logger.info("%s %d: sprint inactive — marked failed", spec.label, row.id)


def _sweep_stale_heartbeats(session, spec: _SweepSpec, now: datetime) -> None:
    """Return running rows with a stale worker heartbeat to pending (or fail)."""
    running = session.exec(select(spec.model).where(spec.model.status == spec.running_status)).all()
    for row in running:
        if not _is_stale(row.last_heartbeat, now, HEARTBEAT_STALE_SECONDS):
            continue
        row.retry_count += 1
        if row.retry_count >= MAX_AUTO_RETRIES:
            row.status = spec.failed_status
            row.error = spec.stale_error
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


def _sweep_pending(session, spec: _SweepSpec, queue_service: Any, now: datetime) -> None:
    """Enqueue pending rows without a live RQ job (finished sprints excluded
    — their rows were failed by the inactive-sprint sweep)."""
    enqueue: Callable[[int], Any] = getattr(queue_service, spec.enqueue_name)
    pending = session.exec(
        spec.join_to_sprint(select(spec.model)).where(
            spec.model.status == spec.pending_status, Sprint.active
        )
    ).all()
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
        for spec in _SWEEP_SPECS:
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
