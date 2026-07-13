"""Tests for backend/services/reconciler.py — direct ``reconcile_once`` calls."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import backend.services.reconciler as reconciler_module
from backend.models.database import Requirement, RequirementStatus
from backend.services.reconciler import reconcile_once
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint


class _StubJob:
    def __init__(self, status: str):
        self._status = status

    def get_status(self):
        return self._status


class _StubQueueService:
    """Recording queue-service stub with controllable job lookups."""

    def __init__(self, available: bool = True, jobs: dict | None = None):
        self.available = available
        self.jobs = jobs or {}
        self.enqueued: list[int] = []

    def enqueue_analysis(self, requirement_id: int):
        self.enqueued.append(requirement_id)
        return SimpleNamespace(id=f"job-{requirement_id}")

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)


@pytest.fixture
def stub_queue(monkeypatch):
    stub = _StubQueueService()
    monkeypatch.setattr(reconciler_module, "get_queue_service", lambda: stub)
    monkeypatch.setattr(reconciler_module, "reset_queue_service", lambda: None)
    return stub


def _reload(db_session, requirement_id) -> Requirement:
    db_session.expire_all()
    return db_session.get(Requirement, requirement_id)


def _stale_time() -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=600)


class TestRedisUnavailable:
    def test_noop_when_redis_down(self, db_session, monkeypatch):
        stub = _StubQueueService(available=False)
        monkeypatch.setattr(reconciler_module, "get_queue_service", lambda: stub)
        monkeypatch.setattr(reconciler_module, "reset_queue_service", lambda: None)

        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint)

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.PENDING
        assert row.job_id is None
        assert stub.enqueued == []

    def test_reconnects_via_reset(self, db_session, monkeypatch):
        dead = _StubQueueService(available=False)
        live = _StubQueueService(available=True)
        services = [dead, live]
        monkeypatch.setattr(reconciler_module, "get_queue_service", lambda: services[0])
        monkeypatch.setattr(reconciler_module, "reset_queue_service", lambda: services.pop(0))

        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint)

        reconcile_once()

        assert live.enqueued == [req.id]


class TestPendingSweep:
    def test_enqueues_pending_without_live_job(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint)

        reconcile_once()

        assert stub_queue.enqueued == [req.id]
        row = _reload(db_session, req.id)
        assert row.job_id == f"job-{req.id}"

    def test_enqueues_pending_whose_job_vanished(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, job_id="gone-job")

        reconcile_once()

        assert stub_queue.enqueued == [req.id]
        row = _reload(db_session, req.id)
        assert row.job_id == f"job-{req.id}"

    def test_skips_pending_with_live_job(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, job_id="live-job")
        stub_queue.jobs["live-job"] = _StubJob("queued")

        reconcile_once()

        assert stub_queue.enqueued == []
        row = _reload(db_session, req.id)
        assert row.job_id == "live-job"

    def test_reenqueues_when_job_finished(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, job_id="done-job")
        stub_queue.jobs["done-job"] = _StubJob("finished")

        reconcile_once()

        assert stub_queue.enqueued == [req.id]

    def test_skips_pending_on_finished_sprint(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session, active=False)
        req = _seed_requirement(db_session, sprint)

        reconcile_once()

        assert stub_queue.enqueued == []
        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.PENDING
        assert row.job_id is None


class TestStaleHeartbeatSweep:
    def test_stale_analyzing_returns_to_pending(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.ANALYZING,
            last_heartbeat=_stale_time(),
        )

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.PENDING
        assert row.retry_count == 1
        assert row.last_heartbeat is None
        # swept back to pending in the same tick → also enqueued
        assert stub_queue.enqueued == [req.id]

    def test_missing_heartbeat_counts_as_stale(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session, sprint, status=RequirementStatus.ANALYZING, last_heartbeat=None
        )

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.PENDING
        assert row.retry_count == 1

    def test_fresh_heartbeat_untouched(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.ANALYZING,
            last_heartbeat=datetime.now(timezone.utc),
        )

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.ANALYZING
        assert row.retry_count == 0
        assert stub_queue.enqueued == []

    def test_retries_exhausted_marks_failed(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.ANALYZING,
            last_heartbeat=_stale_time(),
            retry_count=2,
        )

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.FAILED
        assert row.retry_count == 3
        assert row.error is not None
        assert stub_queue.enqueued == []
