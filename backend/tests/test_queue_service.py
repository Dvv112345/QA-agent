"""Tests for backend/services/queue.py — degradation and singleton behaviour.

The autouse ``_isolate_redis`` fixture in conftest.py neutralises
``QueueService._connect``, so every service here is in the degraded
(Redis-unavailable) state.
"""

from types import SimpleNamespace

import backend.services.queue as queue_module
from backend.config import JOB_TIMEOUT, TEST_PLAN_JOB_TIMEOUT
from backend.services.queue import (
    ANALYZE_REQUIREMENT_TASK,
    GENERATE_TEST_PLAN_TASK,
    REDIS_SOCKET_TIMEOUT,
    QueueService,
    get_queue_service,
    reset_queue_service,
)

# Captured at import time (collection), before the autouse ``_isolate_redis``
# fixture replaces the method on the class for each test.
_ORIGINAL_CONNECT = QueueService._connect


class _RecordingQueue:
    """Stands in for the RQ queue handle and records enqueue calls."""

    def __init__(self):
        self.calls: list[dict] = []

    def enqueue(self, task_path, row_id, **kwargs):
        self.calls.append({"task_path": task_path, "row_id": row_id, **kwargs})
        return SimpleNamespace(id=f"job-{row_id}")


class TestDegradedService:
    def test_unavailable_without_redis(self):
        service = QueueService()
        assert service.available is False
        assert service.get_connection() is None
        assert service.get_queue() is None

    def test_enqueue_returns_none(self):
        service = QueueService()
        assert service.enqueue_analysis(123) is None

    def test_enqueue_test_plan_returns_none(self):
        service = QueueService()
        assert service.enqueue_test_plan(123) is None

    def test_get_job_returns_none(self):
        service = QueueService()
        assert service.get_job("some-job-id") is None

    def test_reset_reconnects_without_error(self):
        service = QueueService()
        service.reset()
        assert service.available is False


class TestConnect:
    def test_socket_timeout_reaches_the_connection_pool(self, monkeypatch):
        """The enqueue-side timeout must be a constructor arg — attribute
        assignment after construction never reaches the connection pool."""
        constructed: dict = {}

        class _StubRedis:
            def __init__(self, **kwargs):
                constructed.update(kwargs)

            def ping(self):
                return True

        monkeypatch.setattr(queue_module.redis, "Redis", _StubRedis)
        monkeypatch.setattr(
            queue_module.rq, "Queue", lambda name, connection: SimpleNamespace(name=name)
        )

        service = QueueService()  # _connect is a no-op here (autouse fixture)
        _ORIGINAL_CONNECT(service)

        assert constructed["socket_timeout"] == REDIS_SOCKET_TIMEOUT
        assert service.available is True


class TestEnqueueDispatch:
    """Both enqueue methods share one path but differ in task + timeout."""

    def test_enqueue_analysis_uses_analysis_task_and_timeout(self):
        service = QueueService()
        queue = _RecordingQueue()
        service._queue = queue

        job = service.enqueue_analysis(9)

        assert job.id == "job-9"
        call = queue.calls[0]
        assert call["task_path"] == ANALYZE_REQUIREMENT_TASK
        assert call["row_id"] == 9
        assert call["job_timeout"] == JOB_TIMEOUT

    def test_enqueue_test_plan_uses_plan_task_and_timeout(self):
        service = QueueService()
        queue = _RecordingQueue()
        service._queue = queue

        job = service.enqueue_test_plan(7)

        assert job.id == "job-7"
        call = queue.calls[0]
        assert call["task_path"] == GENERATE_TEST_PLAN_TASK
        assert call["row_id"] == 7
        assert call["job_timeout"] == TEST_PLAN_JOB_TIMEOUT

    def test_plan_task_dotted_path_matches_module(self):
        # The worker resolves this string at execution time — a rename of the
        # task module/function must update the constant in lockstep.
        assert GENERATE_TEST_PLAN_TASK == (
            "backend.tasks.generate_test_plan.generate_test_plan_task"
        )


class TestSingleton:
    def test_get_queue_service_returns_same_instance(self):
        first = get_queue_service()
        second = get_queue_service()
        assert first is second

    def test_reset_discards_instance(self):
        first = get_queue_service()
        reset_queue_service()
        second = get_queue_service()
        assert first is not second
