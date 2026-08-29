"""Redis-backed job queue for requirement analysis.

Provides a ``QueueService`` that enqueues requirement-analysis jobs.  When
Redis is unavailable the service degrades gracefully: ``enqueue_analysis``
returns ``None`` and requirement rows simply stay ``pending`` until the
reconciler can enqueue them.

All consumers should obtain the shared instance via ``get_queue_service()``
rather than constructing ``QueueService`` directly.  The singleton is
created lazily on first access; ``reset_queue_service()`` discards it so
the next access reconnects, which is how a transient Redis outage is
recovered from.

PostgreSQL is the sole status of record — Redis is transport only, so this
module deliberately has no job-status or job-meta helpers beyond the
``get_job`` dedup lookup used by the reconciler.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import redis
import rq
from sqlmodel import Session

from backend.config import (
    CICD_EXPORT_JOB_TIMEOUT,
    EXPLORATORY_JOB_TIMEOUT,
    JOB_RESULT_TTL,
    JOB_TIMEOUT,
    NONFUNCTIONAL_JOB_TIMEOUT,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PASSWORD,
    REDIS_PORT,
    TEST_EXECUTION_JOB_TIMEOUT,
    TEST_PLAN_JOB_TIMEOUT,
)

logger = logging.getLogger(__name__)

QUEUE_NAME = "qa-jobs"

# Socket timeout for enqueue-side Redis calls (enqueue, ping, job lookups) so
# a dead Redis never hangs a request or a reconciler tick.  RQ raises the
# worker connection's timeout above its blocking-dequeue window on startup,
# so this small value never breaks the worker's BLPOP waits.
REDIS_SOCKET_TIMEOUT = 5

# Dotted paths enqueued instead of function objects so the web process never
# imports the task modules (task modules must not be imported by queue/worker
# modules — circular-import rule).  RQ resolves them to the real functions at
# execution time, so jobs still show their real name in ``rq info``.
ANALYZE_REQUIREMENT_TASK = "backend.tasks.analyze_requirement.analyze_requirement_task"
GENERATE_TEST_PLAN_TASK = "backend.tasks.generate_test_plan.generate_test_plan_task"
EXECUTE_TEST_TASK = "backend.tasks.execute_test.execute_test_task"
EXPLORE_REQUIREMENT_TASK = "backend.tasks.explore_requirement.explore_requirement_task"
CICD_EXPORT_TASK = "backend.tasks.export_cicd.export_cicd_task"
NONFUNCTIONAL_RUN_TASK = "backend.tasks.run_nonfunctional.run_nonfunctional_task"

# ── Module-level singleton ────────────────────────────────────────────────
_queue_service: QueueService | None = None


def enqueue_rows(session: Session, rows: Sequence[Any], enqueue: Callable[[int], Any]) -> None:
    """Best-effort enqueue after commit — failure is the reconciler's job.

    Successful enqueues persist the job id for the reconciler's dedup
    check.  ``enqueue`` is the ``QueueService`` method for this row type,
    e.g. ``get_queue_service().enqueue_analysis``; every stage differs
    only in which one it passes.

    Nothing here raises: with Redis down every call answers ``None``, the
    rows stay ``pending``, and the reconciler picks them up on recovery.
    """
    enqueued = False
    for row in rows:
        job = enqueue(row.id)
        if job is not None:
            row.job_id = job.id
            session.add(row)
            enqueued = True
    if enqueued:
        session.commit()
        for row in rows:
            session.refresh(row)


def get_queue_service() -> QueueService:
    """Return the shared ``QueueService`` singleton, creating it lazily.

    Consumers must call this instead of constructing ``QueueService()``
    themselves so that only one Redis connection pool exists per process.
    """
    global _queue_service
    if _queue_service is None:
        _queue_service = QueueService()
    return _queue_service


def reset_queue_service() -> None:
    """Discard the cached singleton so the next ``get_queue_service()``
    call creates a fresh instance with a new Redis connection.

    Useful when Redis recovers after a transient outage.
    """
    global _queue_service
    _queue_service = None


class QueueService:
    """Thin wrapper around RQ for enqueuing requirement-analysis jobs."""

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None
        self._queue: rq.Queue | None = None
        self._connect()

    def _connect(self) -> None:
        """Attempt to establish a Redis connection and RQ queue handle."""
        try:
            self._redis = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                db=REDIS_DB,
                decode_responses=False,
                socket_timeout=REDIS_SOCKET_TIMEOUT,
            )
            self._redis.ping()
            self._queue = rq.Queue(QUEUE_NAME, connection=self._redis)
            logger.info("Connected to Redis at %s:%d", REDIS_HOST, REDIS_PORT)
        except (redis.exceptions.ConnectionError, OSError) as exc:
            logger.warning("Redis unavailable — analysis jobs will not be enqueued: %s", exc)
            self._redis = None
            self._queue = None

    @property
    def available(self) -> bool:
        """``True`` when Redis is connected and ready."""
        return self._redis is not None and self._queue is not None

    def get_connection(self) -> redis.Redis | None:
        """Return the raw Redis connection (used by the worker CLI)."""
        return self._redis

    def get_queue(self) -> rq.Queue | None:
        """Return the RQ queue handle (used by ``clear_queue``)."""
        return self._queue

    def _enqueue(
        self, task_path: str, row_id: int, job_timeout: int, label: str
    ) -> rq.job.Job | None:
        """Enqueue one job and return the RQ Job handle.

        Returns ``None`` when Redis is unavailable or the enqueue fails —
        the row stays ``pending`` and the reconciler retries later.
        """
        if self._queue is None:
            logger.warning("Cannot enqueue %s for row %d — Redis unavailable", label, row_id)
            return None

        try:
            job = self._queue.enqueue(
                task_path,
                row_id,
                job_timeout=job_timeout,
                result_ttl=JOB_RESULT_TTL,
            )
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning("Enqueue failed for %s %d: %s", label, row_id, exc)
            return None

        logger.info("Enqueued %s job %s for row %d", label, job.id, row_id)
        return job

    def enqueue_analysis(self, requirement_id: int) -> rq.job.Job | None:
        """Enqueue a requirement-analysis job and return the RQ Job handle."""
        return self._enqueue(ANALYZE_REQUIREMENT_TASK, requirement_id, JOB_TIMEOUT, "analysis")

    def enqueue_test_plan(self, test_plan_id: int) -> rq.job.Job | None:
        """Enqueue a test-plan generation job and return the RQ Job handle.

        Plan jobs get their own (much larger) timeout — the tool loop can
        legitimately run many LLM rounds, which ``JOB_TIMEOUT`` would
        hard-kill on Linux.
        """
        return self._enqueue(
            GENERATE_TEST_PLAN_TASK, test_plan_id, TEST_PLAN_JOB_TIMEOUT, "test plan"
        )

    def enqueue_test_execution(self, test_execution_id: int) -> rq.job.Job | None:
        """Enqueue a test-execution job and return the RQ Job handle.

        Execution jobs get the largest timeout — many cases, each with up
        to (1 + MAX_SCRIPT_FIX_ROUNDS) generate/execute/diagnose cycles.
        """
        return self._enqueue(
            EXECUTE_TEST_TASK, test_execution_id, TEST_EXECUTION_JOB_TIMEOUT, "test execution"
        )

    def enqueue_exploration(self, exploratory_run_id: int) -> rq.job.Job | None:
        """Enqueue an exploratory run job and return the RQ Job handle.

        One job covers every charter in the run, serially, so this timeout
        must span the whole set rather than a single session.
        """
        return self._enqueue(
            EXPLORE_REQUIREMENT_TASK,
            exploratory_run_id,
            EXPLORATORY_JOB_TIMEOUT,
            "exploratory run",
        )

    def enqueue_nonfunctional_run(self, nonfunctional_run_id: int) -> rq.job.Job | None:
        """Enqueue a nonfunctional run job and return the RQ Job handle.

        One job covers the whole run — the itinerary, every target's
        catalogue, and every load profile serially — so this timeout must
        span all three rather than a single target.
        """
        return self._enqueue(
            NONFUNCTIONAL_RUN_TASK,
            nonfunctional_run_id,
            NONFUNCTIONAL_JOB_TIMEOUT,
            "nonfunctional run",
        )

    def enqueue_cicd_export(self, cicd_export_id: int) -> rq.job.Job | None:
        """Enqueue a CI/CD export job and return the RQ Job handle.

        One LLM call plus a handful of GitHub requests — smaller than a
        run, larger than an analysis.
        """
        return self._enqueue(
            CICD_EXPORT_TASK, cicd_export_id, CICD_EXPORT_JOB_TIMEOUT, "CI/CD export"
        )

    def get_job(self, job_id: str) -> rq.job.Job | None:
        """Fetch an RQ job by id, or ``None`` when it doesn't exist.

        Used by the reconciler to avoid double-enqueuing a requirement whose
        job is still queued or running.
        """
        if self._redis is None:
            return None
        try:
            return rq.job.Job.fetch(job_id, connection=self._redis)
        except (rq.exceptions.NoSuchJobError, redis.exceptions.RedisError, OSError):
            return None
