"""RQ worker CLI entry point.

Start the worker from the repo root::

    python -m backend.worker

The worker listens on the ``qa-jobs`` queue (defined in
``backend.services.queue.QUEUE_NAME``) and processes requirement-analysis
tasks from ``backend.tasks``.

Multiple worker processes can be started for concurrency::

    # Terminal 1
    python -m backend.worker

    # Terminal 2
    python -m backend.worker

    # Or use a process manager (supervisord, systemd, etc.) to run
    # N workers where N matches your expected concurrency.
"""

from __future__ import annotations

import logging
import sys

from rq import SimpleWorker, Worker

from backend.services.queue import QUEUE_NAME, get_queue_service

# Windows doesn't support os.fork() — use SimpleWorker instead.
_WorkerClass = SimpleWorker if sys.platform == "win32" else Worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] worker: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)


def cli() -> None:
    """Create and start an RQ worker for the ``qa-jobs`` queue."""
    qs = get_queue_service()
    conn = qs.get_connection()

    if conn is None:
        raise RuntimeError(
            "Cannot start worker: Redis is unavailable. "
            "Check REDIS_HOST / REDIS_PORT / REDIS_PASSWORD in your .env file."
        )

    # Socket timeouts: QueueService._connect sets a small enqueue-side one;
    # RQ bumps this connection's timeout above its blocking-dequeue window
    # on startup, so dead-connection hangs are bounded without breaking BLPOP.
    worker = _WorkerClass([QUEUE_NAME], connection=conn)
    logging.getLogger("rq.worker").info("RQ worker started — listening on queue '%s'", QUEUE_NAME)
    worker.work()


if __name__ == "__main__":
    cli()
