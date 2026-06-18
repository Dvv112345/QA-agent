"""Clear all jobs from the RQ queue and Redis job registries.

Usage::

    python -m backend.clear_queue
"""

from __future__ import annotations

import logging

from rq.registry import FailedJobRegistry, FinishedJobRegistry, StartedJobRegistry

from backend.services.queue import QUEUE_NAME, get_queue_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] clear_queue: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


def cli() -> None:
    qs = get_queue_service()
    conn = qs.get_connection()

    if conn is None:
        raise RuntimeError(
            "Redis is unavailable. "
            "Check REDIS_HOST / REDIS_PORT / REDIS_PASSWORD in your .env file."
        )

    queue = qs.get_queue()

    # Count before clearing
    queued = len(queue)
    started = StartedJobRegistry(QUEUE_NAME, connection=conn).count
    finished = FinishedJobRegistry(QUEUE_NAME, connection=conn).count
    failed = FailedJobRegistry(QUEUE_NAME, connection=conn).count

    total = queued + started + finished + failed
    log.info(
        "Queue '%s': %d queued, %d started, %d finished, %d failed (%d total)",
        QUEUE_NAME,
        queued,
        started,
        finished,
        failed,
        total,
    )

    if total == 0:
        log.info("Nothing to clear.")
        return

    # Empty the queue and clear registries
    queue.empty()
    for registry_cls in (StartedJobRegistry, FinishedJobRegistry, FailedJobRegistry):
        registry = registry_cls(QUEUE_NAME, connection=conn)
        for job_id in registry.get_job_ids():
            registry.remove(job_id)

    log.info("Cleared %d job(s).", total)


if __name__ == "__main__":
    cli()
