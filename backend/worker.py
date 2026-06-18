"""RQ worker CLI entry point.

Start the worker from the repo root::

    python -m backend.worker

The worker listens on the ``qa-jobs`` queue (defined in
``backend.services.queue.QUEUE_NAME``) and processes word-count tasks
defined in ``backend.tasks``.
"""

from __future__ import annotations

import logging

from rq import Worker

from backend.services.queue import QUEUE_NAME, QueueService


def cli() -> None:
    """Create and start an RQ worker for the ``qa-jobs`` queue."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] worker: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    queue_service = QueueService()
    conn = queue_service.get_connection()

    if conn is None:
        raise RuntimeError(
            "Cannot start worker: Redis is unavailable. "
            "Check REDIS_HOST / REDIS_PORT / REDIS_PASSWORD in your .env file."
        )

    worker = Worker([QUEUE_NAME], connection=conn)
    logging.getLogger("rq.worker").info("RQ worker started — listening on queue '%s'", QUEUE_NAME)
    worker.work()


if __name__ == "__main__":
    cli()
