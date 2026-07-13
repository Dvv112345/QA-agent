"""Reconciler — re-enqueues requirement analysis lost to Redis or worker crashes.

Runs as an asyncio background task started by the FastAPI lifespan.  Each
tick (``reconcile_once``) does three things:

1. If Redis was down, rebuild the queue-service singleton (reconnect).
2. Sweep ``analyzing`` rows whose worker heartbeat went stale (crashed
   worker) back to ``pending`` — or to ``failed`` once auto-retries are
   exhausted.
3. Enqueue every ``pending`` row that has no live RQ job.

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
from backend.models.database import Requirement, RequirementStatus
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
            logger.debug("Reconciler: Redis unavailable — nothing to do")
            return

    now = datetime.now(timezone.utc)
    with new_session() as session:
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
        pending = session.exec(
            select(Requirement).where(Requirement.status == RequirementStatus.PENDING)
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
