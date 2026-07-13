"""Redis-backed job queue for requirement analysis.

Provides a ``QueueService`` that enqueues requirement-analysis jobs.  When
Redis is unavailable the service degrades gracefully: ``enqueue_analysis``
returns ``None`` and requirement rows simply stay ``pending`` until the
reconciler can enqueue them.

All consumers should obtain the shared instance via ``get_queue_service()``
rather than constructing ``QueueService`` directly.  The singleton is
created lazily on first access and supports ``reset()`` for reconnection
when Redis recovers after a transient outage.

PostgreSQL is the sole status of record — Redis is transport only, so this
module deliberately has no job-status or job-meta helpers beyond the
``get_job`` dedup lookup used by the reconciler.
"""

from __future__ import annotations

import contextlib
import logging

import redis
import rq

from backend.config import (
    JOB_RESULT_TTL,
    JOB_TIMEOUT,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PASSWORD,
    REDIS_PORT,
)

logger = logging.getLogger(__name__)

QUEUE_NAME = "qa-jobs"

# Dotted path enqueued instead of a function object so the web process never
# imports the task module (task modules must not be imported by queue/worker
# modules — circular-import rule).  RQ resolves it to the real function at
# execution time, so jobs still show their real name in ``rq info``.
ANALYZE_REQUIREMENT_TASK = "backend.tasks.analyze_requirement.analyze_requirement_task"

# ── Module-level singleton ────────────────────────────────────────────────
_queue_service: QueueService | None = None


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

    def reset(self) -> None:
        """Close the current connection (if any) and reconnect.

        Call this after detecting that Redis has become available following
        a transient outage.
        """
        if self._redis:
            with contextlib.suppress(Exception):
                self._redis.close()
        self._redis = None
        self._queue = None
        self._connect()

    def get_connection(self) -> redis.Redis | None:
        """Return the raw Redis connection (used by the worker CLI)."""
        return self._redis

    def get_queue(self) -> rq.Queue | None:
        """Return the RQ queue handle (used by ``clear_queue``)."""
        return self._queue

    def enqueue_analysis(self, requirement_id: int) -> rq.job.Job | None:
        """Enqueue a requirement-analysis job and return the RQ Job handle.

        Returns ``None`` when Redis is unavailable or the enqueue fails —
        the row stays ``pending`` and the reconciler retries later.
        """
        if self._queue is None:
            logger.warning(
                "Cannot enqueue analysis for requirement %d — Redis unavailable",
                requirement_id,
            )
            return None

        try:
            job = self._queue.enqueue(
                ANALYZE_REQUIREMENT_TASK,
                requirement_id,
                job_timeout=JOB_TIMEOUT,
                result_ttl=JOB_RESULT_TTL,
            )
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning("Enqueue failed for requirement %d: %s", requirement_id, exc)
            return None

        logger.info("Enqueued analysis job %s for requirement %d", job.id, requirement_id)
        return job

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
