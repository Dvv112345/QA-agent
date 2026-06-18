"""Redis-backed job queue for word-count processing.

Provides a ``QueueService`` that enqueues word-count jobs and looks up
their status.  When Redis is unavailable the service degrades gracefully:
``enqueue_word_count`` returns ``None`` and ``get_job_status`` returns
``None``.
"""

from __future__ import annotations

import logging

import redis
import rq

from backend.config import JOB_TIMEOUT, REDIS_DB, REDIS_HOST, REDIS_PASSWORD, REDIS_PORT

logger = logging.getLogger(__name__)

QUEUE_NAME = "qa-jobs"


class QueueService:
    """Thin wrapper around RQ for enqueuing and tracking word-count jobs."""

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None
        self._queue: rq.Queue | None = None

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
            logger.warning("Redis unavailable — word-count jobs will not be enqueued: %s", exc)
            self._redis = None
            self._queue = None

    def get_connection(self) -> redis.Redis | None:
        """Return the raw Redis connection (used by the worker CLI)."""
        return self._redis

    def enqueue_word_count(
        self,
        job_id: str,
        md_path: str,
        zip_path: str,
        files: list[str],
    ) -> rq.job.Job | None:
        """Enqueue a word-count job and return the RQ Job handle.

        Returns ``None`` when Redis is unavailable.
        """
        if self._queue is None:
            logger.warning("Cannot enqueue word-count job — Redis unavailable")
            return None

        from backend.tasks import count_words_task

        job = self._queue.enqueue(
            count_words_task,
            job_id,
            md_path,
            zip_path,
            files,
            JOB_TIMEOUT,
            job_id=job_id,
            job_timeout=JOB_TIMEOUT,
            result_ttl=3600,
        )
        # Set total_files in meta so the frontend sees it from the first poll.
        job.meta["total_files"] = len(files)
        job.save_meta()

        logger.info("Enqueued word-count job %s (%d files)", job_id, len(files))
        return job

    def get_job_status(self, job_id: str) -> dict | None:
        """Return job progress metadata, or ``None`` if the job doesn't exist.

        Return shape::

            {
                "status": "queued" | "started" | "finished" | "failed",
                "total_files": int,
                "processed_files": int,
                "error": str | None,
                "result": dict | None,   # only when status == "finished"
            }
        """
        if self._redis is None:
            return None

        try:
            job = rq.job.Job.fetch(job_id, connection=self._redis)
        except rq.exceptions.NoSuchJobError:
            return None

        status = job.get_status(refresh=False) or "unknown"
        total_files = (job.meta or {}).get("total_files", 0)
        processed_files = (job.meta or {}).get("processed_files", 0)

        response: dict = {
            "status": status,
            "total_files": total_files,
            "processed_files": processed_files,
            "error": None,
            "result": None,
        }

        if status == "failed" and job.exc_info:
            response["error"] = str(job.exc_info)
        elif status == "finished":
            response["result"] = job.result

        return response
