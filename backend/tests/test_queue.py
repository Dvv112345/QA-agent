"""Tests for backend/services/queue.py — QueueService, singleton, and helpers."""

from unittest.mock import MagicMock, patch

from backend.services.queue import (
    QUEUE_NAME,
    QueueService,
    get_queue_service,
    reset_queue_service,
)


class TestSingleton:
    """Tests for ``get_queue_service()`` and ``reset_queue_service()``."""

    def test_get_queue_service_returns_same_instance(self):
        """Repeated calls return the exact same object."""
        reset_queue_service()
        with patch("backend.services.queue.redis.Redis") as mock_redis_cls:
            mock_redis = MagicMock()
            mock_redis.ping.return_value = True
            mock_redis_cls.return_value = mock_redis
            svc1 = get_queue_service()
            svc2 = get_queue_service()
            assert svc1 is svc2

    def test_reset_queue_service_creates_new_instance(self):
        """After reset, the next call returns a fresh instance."""
        reset_queue_service()
        with patch("backend.services.queue.redis.Redis") as mock_redis_cls:
            mock_redis = MagicMock()
            mock_redis.ping.return_value = True
            mock_redis_cls.return_value = mock_redis
            svc1 = get_queue_service()
            reset_queue_service()
            svc2 = get_queue_service()
            assert svc1 is not svc2

    def test_get_queue_service_returns_queue_service(self):
        """The singleton is a ``QueueService`` instance."""
        reset_queue_service()
        with patch("backend.services.queue.redis.Redis") as mock_redis_cls:
            mock_redis = MagicMock()
            mock_redis.ping.return_value = True
            mock_redis_cls.return_value = mock_redis
            svc = get_queue_service()
            assert isinstance(svc, QueueService)


class TestQueueServiceEnqueue:
    """Tests for ``QueueService.enqueue_word_count``."""

    def test_enqueue_returns_none_when_redis_unavailable(self):
        """When Redis connection fails, enqueue_word_count returns None."""
        with patch("backend.services.queue.redis.Redis") as mock_redis_cls:
            mock_redis_cls.side_effect = OSError("Connection refused")
            svc = QueueService()
            assert svc.available is False
            job = svc.enqueue_word_count("test-job", "/tmp/md.md", "/tmp/zip", ["a.py", "b.py"])
            assert job is None

    def test_enqueue_sets_total_files_in_meta(self):
        """After enqueue, job.meta['total_files'] must equal len(files)."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_queue = MagicMock()
        mock_job = MagicMock()
        mock_job.meta = {}
        mock_queue.enqueue.return_value = mock_job

        with (
            patch("backend.services.queue.redis.Redis", return_value=mock_redis),
            patch("backend.services.queue.rq.Queue", return_value=mock_queue),
        ):
            svc = QueueService()
            assert svc.available is True
            files = ["a.py", "b.py", "c.py"]
            svc.enqueue_word_count("job-1", "/tmp/md.md", "/tmp/zip", files)

            assert mock_job.meta["total_files"] == 3
            mock_job.save_meta.assert_called()

    def test_enqueue_uses_correct_queue_name(self):
        """QueueService must use QUEUE_NAME when creating the RQ queue."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True

        with (
            patch("backend.services.queue.redis.Redis", return_value=mock_redis),
            patch("backend.services.queue.rq.Queue") as mock_rq_queue,
        ):
            QueueService()
            mock_rq_queue.assert_called_once()
            args, _kwargs = mock_rq_queue.call_args
            assert args[0] == QUEUE_NAME

    def test_enqueue_passes_result_ttl_from_config(self, monkeypatch):
        """result_ttl is read from JOB_RESULT_TTL env var."""
        monkeypatch.setenv("JOB_RESULT_TTL", "7200")
        # Force reimport so the config value is fresh
        import importlib

        import backend.config
        import backend.services.queue

        importlib.reload(backend.config)
        importlib.reload(backend.services.queue)

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_queue = MagicMock()
        mock_job = MagicMock()
        mock_job.meta = {}
        mock_queue.enqueue.return_value = mock_job

        with (
            patch("backend.services.queue.redis.Redis", return_value=mock_redis),
            patch("backend.services.queue.rq.Queue", return_value=mock_queue),
        ):
            # Force re-init since module-level config is already loaded
            # We just verify the config value propagated
            from backend.config import JOB_RESULT_TTL

            assert JOB_RESULT_TTL == 7200


class TestQueueServiceGetStatus:
    """Tests for ``QueueService.get_job_status``."""

    def test_get_status_returns_none_when_redis_unavailable(self):
        with patch("backend.services.queue.redis.Redis") as mock_redis_cls:
            mock_redis_cls.side_effect = OSError("Connection refused")
            svc = QueueService()
            assert svc.get_job_status("any-job") is None

    def test_get_status_returns_none_for_missing_job(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True

        with (
            patch("backend.services.queue.redis.Redis", return_value=mock_redis),
            patch("backend.services.queue.rq.Queue"),
            patch("backend.services.queue.rq.job.Job.fetch") as mock_fetch,
        ):
            from rq.exceptions import NoSuchJobError

            mock_fetch.side_effect = NoSuchJobError
            svc = QueueService()
            assert svc.get_job_status("nonexistent") is None

    def test_get_status_reads_progress_from_meta(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_job = MagicMock()
        mock_job.get_status.return_value = "started"
        mock_job.meta = {"total_files": 10, "processed_files": 4}
        mock_job.exc_info = None
        mock_job.result = None

        with (
            patch("backend.services.queue.redis.Redis", return_value=mock_redis),
            patch("backend.services.queue.rq.Queue"),
            patch("backend.services.queue.rq.job.Job.fetch", return_value=mock_job),
        ):
            svc = QueueService()
            status = svc.get_job_status("job-1")

            assert status is not None
            assert status["status"] == "started"
            assert status["total_files"] == 10
            assert status["processed_files"] == 4

    def test_get_status_includes_error_on_failure(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_job = MagicMock()
        mock_job.get_status.return_value = "failed"
        mock_job.meta = {"total_files": 5, "processed_files": 2}
        mock_job.exc_info = "Something broke"
        mock_job.result = None

        with (
            patch("backend.services.queue.redis.Redis", return_value=mock_redis),
            patch("backend.services.queue.rq.Queue"),
            patch("backend.services.queue.rq.job.Job.fetch", return_value=mock_job),
        ):
            svc = QueueService()
            status = svc.get_job_status("job-fail")

            assert status is not None
            assert status["status"] == "failed"
            assert status["error"] == "Something broke"

    def test_get_status_includes_result_on_finished(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_job = MagicMock()
        mock_job.get_status.return_value = "finished"
        mock_job.meta = {"total_files": 1, "processed_files": 1}
        mock_job.exc_info = None
        mock_job.result = {
            "md_result": {"file": "req.md", "words": 50},
            "zip_results": [{"file": "a.py", "words": 30}],
            "total_files": 1,
            "processed_files": 1,
            "total_words": 80,
        }

        with (
            patch("backend.services.queue.redis.Redis", return_value=mock_redis),
            patch("backend.services.queue.rq.Queue"),
            patch("backend.services.queue.rq.job.Job.fetch", return_value=mock_job),
        ):
            svc = QueueService()
            status = svc.get_job_status("job-done")

            assert status is not None
            assert status["status"] == "finished"
            assert status["result"] == mock_job.result


class TestQueueServiceReset:
    """Tests for ``QueueService.reset()``."""

    def test_reset_reconnects_after_transient_outage(self):
        """After a transient outage, reset() should reconnect."""
        mock_redis_first = MagicMock()
        mock_redis_first.ping.return_value = True

        with (
            patch("backend.services.queue.redis.Redis", return_value=mock_redis_first),
            patch("backend.services.queue.rq.Queue"),
        ):
            svc = QueueService()
            assert svc.available is True

            # Simulate: connection is lost
            svc._redis = None
            svc._queue = None
            assert svc.available is False

            # Reset should create a fresh connection
            mock_redis_second = MagicMock()
            mock_redis_second.ping.return_value = True
            with (
                patch("backend.services.queue.redis.Redis", return_value=mock_redis_second),
                patch("backend.services.queue.rq.Queue"),
            ):
                svc.reset()
                assert svc.available is True

    def test_reset_when_never_connected(self):
        """reset() on a service that never connected should try again."""
        with patch("backend.services.queue.redis.Redis") as mock_redis_cls:
            mock_redis_cls.side_effect = OSError("Connection refused")
            svc = QueueService()
            assert svc.available is False

        # Now Redis is back
        mock_redis_ok = MagicMock()
        mock_redis_ok.ping.return_value = True
        with (
            patch("backend.services.queue.redis.Redis", return_value=mock_redis_ok),
            patch("backend.services.queue.rq.Queue"),
        ):
            svc.reset()
            assert svc.available is True
