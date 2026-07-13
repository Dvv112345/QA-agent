"""Tests for backend/services/queue.py — degradation and singleton behaviour.

The autouse ``_isolate_redis`` fixture in conftest.py neutralises
``QueueService._connect``, so every service here is in the degraded
(Redis-unavailable) state.
"""

from backend.services.queue import (
    QueueService,
    get_queue_service,
    reset_queue_service,
)


class TestDegradedService:
    def test_unavailable_without_redis(self):
        service = QueueService()
        assert service.available is False
        assert service.get_connection() is None
        assert service.get_queue() is None

    def test_enqueue_returns_none(self):
        service = QueueService()
        assert service.enqueue_analysis(123) is None

    def test_get_job_returns_none(self):
        service = QueueService()
        assert service.get_job("some-job-id") is None

    def test_reset_reconnects_without_error(self):
        service = QueueService()
        service.reset()
        assert service.available is False


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
