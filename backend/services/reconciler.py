"""Reconciler — re-enqueues requirement analysis lost to Redis or worker crashes.

Runs as an asyncio background task started by the FastAPI lifespan.  Each
tick (``reconcile_once``) does four things:

1. If Redis was down, rebuild the queue-service singleton (reconnect).
2. Fail ``pending``/``analyzing`` rows whose sprint is inactive — races
   around sprint finish can recreate them after ``finish_sprint``'s own
   sweep, and nothing may stay in-progress on a finished sprint.
3. Sweep ``analyzing`` rows whose worker heartbeat went stale (crashed
   worker) back to ``pending`` — or to ``failed`` once auto-retries are
   exhausted.
4. Enqueue every ``pending`` row that has no live RQ job.

The database sweeps (2–3) run even while Redis is down; only the enqueue
sweep (4) needs the queue.

PostgreSQL is the status of record, so a tick is idempotent and safe to run
concurrently with user actions: the task's own status guard skips rows the
user confirmed or edited meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlmodel import select

from backend.config import HEARTBEAT_STALE_SECONDS, MAX_AUTO_RETRIES, RECONCILER_INTERVAL
from backend.database import new_session
from backend.models.database import (
    SPRINT_FINISHED_ERROR,
    Requirement,
    RequirementStatus,
    Sprint,
)
from backend.services.queue import get_queue_service, reset_queue_service

logger = logging.getLogger(__name__)

_STALE_WORKER_ERROR = (
    "Analysis worker died repeatedly while processing this requirement. Use Restart to try again."
)

# RQ job states that mean "already in flight — don't enqueue again".
_LIVE_JOB_STATES = ("queued", "started")


def _is_stale(heartbeat: datetime | None, now: datetime) -> bool:
    """Whether an ``analyzing`` row's worker heartbeat is too old to trust.

    SQLite (tests) and timestamp-without-timezone columns return naive
    datetimes — normalise to aware UTC before comparing.
    """
    if heartbeat is None:
        return True
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    return (now - heartbeat).total_seconds() > HEARTBEAT_STALE_SECONDS


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
        # ── Inactive-sprint sweep ─────────────────────────────────────
        # finish_sprint fails in-progress rows in its own commit, but races
        # around the finish can recreate them (a task failure re-pending a
        # row, an analyzing row the finish sweep missed).  Converge them to
        # the same failed state so nothing stays in-progress forever on a
        # finished sprint.  Runs before the stale-heartbeat sweep so such
        # rows are failed, not re-pended.
        orphaned = session.exec(
            select(Requirement)
            .join(Sprint)
            .where(
                Requirement.status.in_(  # type: ignore[attr-defined]
                    [RequirementStatus.PENDING, RequirementStatus.ANALYZING]
                ),
                Sprint.active.is_(False),  # type: ignore[attr-defined]
            )
        ).all()
        for row in orphaned:
            row.status = RequirementStatus.FAILED
            row.error = SPRINT_FINISHED_ERROR
            row.last_heartbeat = None
            row.pending_answer = None
            row.updated_at = now
            session.add(row)
            logger.info("Requirement %d: sprint inactive — marked failed", row.id)

        # ── Stale-heartbeat sweep (crashed workers) ───────────────────
        analyzing = session.exec(
            select(Requirement).where(Requirement.status == RequirementStatus.ANALYZING)
        ).all()
        for row in analyzing:
            if not _is_stale(row.last_heartbeat, now):
                continue
            row.retry_count += 1
            if row.retry_count >= MAX_AUTO_RETRIES:
                row.status = RequirementStatus.FAILED
                row.error = _STALE_WORKER_ERROR
                logger.warning(
                    "Requirement %d: worker heartbeat stale, retries exhausted → failed", row.id
                )
            else:
                row.status = RequirementStatus.PENDING
                logger.info(
                    "Requirement %d: worker heartbeat stale → pending (retry %d)",
                    row.id,
                    row.retry_count,
                )
            row.last_heartbeat = None
            row.updated_at = now
            session.add(row)

        # ── Pending sweep (enqueue backlog) ───────────────────────────
        # Finished sprints are excluded (their rows were failed above).
        if queue_service.available:
            pending = session.exec(
                select(Requirement)
                .join(Sprint)
                .where(Requirement.status == RequirementStatus.PENDING, Sprint.active)
            ).all()
            for row in pending:
                if row.job_id:
                    job = queue_service.get_job(row.job_id)
                    if job is not None and job.get_status() in _LIVE_JOB_STATES:
                        continue  # already in flight — dedup
                job = queue_service.enqueue_analysis(row.id)
                if job is not None:
                    row.job_id = job.id
                    row.updated_at = now
                    session.add(row)
                    logger.info("Reconciler enqueued requirement %d as job %s", row.id, job.id)

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
