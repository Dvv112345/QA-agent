"""Tests for backend/services/reconciler.py — direct ``reconcile_once`` calls."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import select

import backend.services.reconciler as reconciler_module
from backend.models.database import SPRINT_FINISHED_ERROR, Requirement, RequirementStatus
from backend.services.reconciler import reconcile_once
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint


class _StubJob:
    def __init__(self, status: str, started_at: datetime | None = None):
        self._status = status
        self.started_at = started_at

    def get_status(self):
        return self._status


class _StubQueueService:
    """Recording queue-service stub with controllable job lookups."""

    def __init__(self, available: bool = True, jobs: dict | None = None):
        self.available = available
        self.jobs = jobs or {}
        self.enqueued: list[int] = []
        self.enqueued_plans: list[int] = []
        self.enqueued_executions: list[int] = []
        self.enqueued_explorations: list[int] = []

    def enqueue_analysis(self, requirement_id: int):
        self.enqueued.append(requirement_id)
        return SimpleNamespace(id=f"job-{requirement_id}")

    def enqueue_test_plan(self, test_plan_id: int):
        self.enqueued_plans.append(test_plan_id)
        return SimpleNamespace(id=f"plan-job-{test_plan_id}")

    def enqueue_test_execution(self, test_execution_id: int):
        self.enqueued_executions.append(test_execution_id)
        return SimpleNamespace(id=f"execution-job-{test_execution_id}")

    def enqueue_exploration(self, exploratory_run_id: int):
        self.enqueued_explorations.append(exploratory_run_id)
        return SimpleNamespace(id=f"exploration-job-{exploratory_run_id}")

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
    def test_no_enqueue_when_redis_down(self, db_session, monkeypatch):
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

    def test_db_sweeps_run_when_redis_down(self, db_session, monkeypatch):
        """The stale-heartbeat and inactive-sprint sweeps don't need the queue."""
        stub = _StubQueueService(available=False)
        monkeypatch.setattr(reconciler_module, "get_queue_service", lambda: stub)
        monkeypatch.setattr(reconciler_module, "reset_queue_service", lambda: None)

        active = _seed_sprint(db_session)
        stale = _seed_requirement(
            db_session,
            active,
            status=RequirementStatus.ANALYZING,
            last_heartbeat=_stale_time(),
        )
        finished = _seed_sprint(db_session, active=False)
        orphaned = _seed_requirement(db_session, finished)

        reconcile_once()

        stale_row = _reload(db_session, stale.id)
        assert stale_row.status == RequirementStatus.PENDING
        assert stale_row.retry_count == 1
        orphaned_row = _reload(db_session, orphaned.id)
        assert orphaned_row.status == RequirementStatus.FAILED
        assert orphaned_row.error == SPRINT_FINISHED_ERROR
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

    def test_enqueues_an_archived_requirement_so_it_can_converge(self, db_session, stub_queue):
        """Deliberately *not* filtered out.

        Skipping it left the row swept by nothing — never enqueued, so never
        picked up by the task that fails it, and never failed by the other
        sweeps either. The task refuses an archived requirement before
        spending any LLM call, so this costs a no-op job and reaches a
        terminal state, which is the cheaper mistake.
        """
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint)
        req.archived = True
        db_session.add(req)
        db_session.commit()

        reconcile_once()

        assert stub_queue.enqueued == [req.id]


class TestInactiveSprintSweep:
    def test_fails_pending_on_finished_sprint(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session, active=False)
        req = _seed_requirement(db_session, sprint, pending_answer="stale answer")

        reconcile_once()

        assert stub_queue.enqueued == []
        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.FAILED
        assert row.error == SPRINT_FINISHED_ERROR
        assert row.pending_answer is None
        assert row.retry_count == 0

    def test_fails_stale_analyzing_on_finished_sprint(self, db_session, stub_queue):
        """Runs before the stale-heartbeat sweep — failed, never re-pended."""
        sprint = _seed_sprint(db_session, active=False)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.ANALYZING,
            last_heartbeat=_stale_time(),
        )

        reconcile_once()

        assert stub_queue.enqueued == []
        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.FAILED
        assert row.error == SPRINT_FINISHED_ERROR
        assert row.retry_count == 0
        assert row.last_heartbeat is None

    def test_fails_fresh_analyzing_on_finished_sprint(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session, active=False)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.ANALYZING,
            last_heartbeat=datetime.now(timezone.utc),
        )

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.FAILED
        assert row.error == SPRINT_FINISHED_ERROR

    def test_terminal_rows_on_finished_sprint_untouched(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session, active=False)
        seeded = {
            _seed_requirement(db_session, sprint, status=status).id: status
            for status in (
                RequirementStatus.NEEDS_CLARIFICATION,
                RequirementStatus.READY,
                RequirementStatus.CONFIRMED,
                RequirementStatus.FAILED,
            )
        }

        reconcile_once()

        for req_id, status in seeded.items():
            row = _reload(db_session, req_id)
            assert row.status == status
            assert row.error is None


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


class TestStalePendingJobSweep:
    """Pending rows whose RQ job started but the worker crashed before the
    task's first commit flipped the row to analyzing."""

    def test_stale_started_job_retries_to_pending_and_reenqueues(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, job_id="crashed-job")
        stub_queue.jobs["crashed-job"] = _StubJob("started", started_at=_stale_time())

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.PENDING
        assert row.retry_count == 1
        assert row.job_id == f"job-{req.id}"
        assert stub_queue.enqueued == [req.id]

    def test_stale_started_job_exhausted_retries_marks_failed(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, job_id="crashed-job", retry_count=2)
        stub_queue.jobs["crashed-job"] = _StubJob("started", started_at=_stale_time())

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.FAILED
        assert row.retry_count == 3
        assert row.error is not None
        assert row.job_id is None
        assert stub_queue.enqueued == []

    def test_fresh_started_job_not_touched(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, job_id="live-job")
        stub_queue.jobs["live-job"] = _StubJob("started", started_at=datetime.now(timezone.utc))

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.PENDING
        assert row.retry_count == 0
        assert row.job_id == "live-job"
        assert stub_queue.enqueued == []

    def test_started_job_missing_started_at_treated_as_stale(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, job_id="crashed-job")
        stub_queue.jobs["crashed-job"] = _StubJob("started", started_at=None)

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.PENDING
        assert row.retry_count == 1
        assert stub_queue.enqueued == [req.id]

    def test_queued_job_never_treated_as_stale(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, job_id="queued-job")
        # Queued jobs have no started_at in RQ; a stray value must still be ignored.
        stub_queue.jobs["queued-job"] = _StubJob("queued", started_at=_stale_time())

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.PENDING
        assert row.retry_count == 0
        assert row.job_id == "queued-job"
        assert stub_queue.enqueued == []

    def test_recycled_analyzing_row_not_double_counted(self, db_session, stub_queue):
        """A single crash mid-analysis must only increment retry_count once,
        even though both the stale-heartbeat sweep and the pending sweep's
        job-status check would otherwise independently detect it."""
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.ANALYZING,
            last_heartbeat=_stale_time(),
            job_id="crashed-job",
        )
        stub_queue.jobs["crashed-job"] = _StubJob("started", started_at=_stale_time())

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.PENDING
        assert row.retry_count == 1
        assert row.job_id == f"job-{req.id}"
        assert stub_queue.enqueued == [req.id]

    def test_recycled_analyzing_row_exhaustion_not_double_counted(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.ANALYZING,
            last_heartbeat=_stale_time(),
            job_id="crashed-job",
            retry_count=2,
        )
        stub_queue.jobs["crashed-job"] = _StubJob("started", started_at=_stale_time())

        reconcile_once()

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.FAILED
        assert row.retry_count == 3
        assert row.job_id is None
        assert stub_queue.enqueued == []


# == Test-plan sweeps (mirroring the requirement sweeps) ===============


def _seed_plan(db_session, sprint, status=None, **kwargs):
    """A confirmed requirement + its test plan on *sprint*."""
    from backend.models.database import TestPlanStatus
    from backend.tests.test_sprints import _seed_test_plan

    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    return _seed_test_plan(
        db_session, requirement, status=status or TestPlanStatus.PENDING, **kwargs
    )


def _reload_plan(db_session, plan_id):
    from backend.models.database import TestPlan

    db_session.expire_all()
    return db_session.get(TestPlan, plan_id)


class TestTestPlanSweeps:
    def test_fails_in_progress_plans_on_finished_sprint(self, db_session, stub_queue):
        from backend.models.database import TestPlanStatus

        sprint = _seed_sprint(db_session, active=False)
        pending = _seed_plan(db_session, sprint, pending_feedback="stale feedback")
        generating = _seed_plan(
            db_session,
            sprint,
            status=TestPlanStatus.GENERATING,
            last_heartbeat=_stale_time(),
        )

        reconcile_once()

        for plan_id in (pending.id, generating.id):
            row = _reload_plan(db_session, plan_id)
            assert row.status == TestPlanStatus.FAILED
            assert row.error == SPRINT_FINISHED_ERROR
            assert row.pending_feedback is None
            assert row.last_heartbeat is None
            assert row.retry_count == 0
        assert stub_queue.enqueued_plans == []

    def test_settled_plans_on_finished_sprint_untouched(self, db_session, stub_queue):
        from backend.models.database import TestPlanStatus

        sprint = _seed_sprint(db_session, active=False)
        seeded = {
            _seed_plan(db_session, sprint, status=status).id: status
            for status in (
                TestPlanStatus.DRAFT,
                TestPlanStatus.APPROVED,
                TestPlanStatus.FAILED,
            )
        }

        reconcile_once()

        for plan_id, status in seeded.items():
            row = _reload_plan(db_session, plan_id)
            assert row.status == status
            assert row.error is None

    def test_stale_generating_plan_returns_to_pending_and_reenqueues(self, db_session, stub_queue):
        from backend.models.database import TestPlanStatus

        sprint = _seed_sprint(db_session)
        plan = _seed_plan(
            db_session,
            sprint,
            status=TestPlanStatus.GENERATING,
            last_heartbeat=_stale_time(),
            job_id="crashed-job",
        )

        reconcile_once()

        row = _reload_plan(db_session, plan.id)
        assert row.status == TestPlanStatus.PENDING
        assert row.retry_count == 1
        assert row.last_heartbeat is None
        assert row.job_id == f"plan-job-{plan.id}"
        assert stub_queue.enqueued_plans == [plan.id]

    def test_stale_generating_plan_exhausted_marks_failed(self, db_session, stub_queue):
        from backend.models.database import TestPlanStatus

        sprint = _seed_sprint(db_session)
        plan = _seed_plan(
            db_session,
            sprint,
            status=TestPlanStatus.GENERATING,
            last_heartbeat=_stale_time(),
            retry_count=2,
        )

        reconcile_once()

        row = _reload_plan(db_session, plan.id)
        assert row.status == TestPlanStatus.FAILED
        assert row.retry_count == 3
        assert row.error is not None
        assert stub_queue.enqueued_plans == []

    def test_fresh_generating_plan_untouched(self, db_session, stub_queue):
        from backend.models.database import TestPlanStatus

        sprint = _seed_sprint(db_session)
        plan = _seed_plan(
            db_session,
            sprint,
            status=TestPlanStatus.GENERATING,
            last_heartbeat=datetime.now(timezone.utc),
        )

        reconcile_once()

        row = _reload_plan(db_session, plan.id)
        assert row.status == TestPlanStatus.GENERATING
        assert row.retry_count == 0
        assert stub_queue.enqueued_plans == []

    def test_enqueues_pending_plan_and_persists_job_id(self, db_session, stub_queue):
        from backend.models.database import TestPlanStatus

        sprint = _seed_sprint(db_session)
        plan = _seed_plan(db_session, sprint)

        reconcile_once()

        assert stub_queue.enqueued_plans == [plan.id]
        row = _reload_plan(db_session, plan.id)
        assert row.status == TestPlanStatus.PENDING
        assert row.job_id == f"plan-job-{plan.id}"

    def test_skips_pending_plan_with_live_job(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        plan = _seed_plan(db_session, sprint, job_id="live-plan-job")
        stub_queue.jobs["live-plan-job"] = _StubJob("queued")

        reconcile_once()

        assert stub_queue.enqueued_plans == []
        row = _reload_plan(db_session, plan.id)
        assert row.job_id == "live-plan-job"

    def test_stale_started_plan_job_retries(self, db_session, stub_queue):
        """The pending sweep's crashed-before-generating detection applies
        to plan jobs exactly as it does to analysis jobs."""
        sprint = _seed_sprint(db_session)
        plan = _seed_plan(db_session, sprint, job_id="crashed-plan-job")
        stub_queue.jobs["crashed-plan-job"] = _StubJob("started", started_at=_stale_time())

        reconcile_once()

        row = _reload_plan(db_session, plan.id)
        assert row.retry_count == 1
        assert row.job_id == f"plan-job-{plan.id}"
        assert stub_queue.enqueued_plans == [plan.id]

    def test_requirement_and_plan_sweeps_share_a_tick(self, db_session, stub_queue):
        """Both row types are handled in the same reconcile_once call."""
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint)
        plan = _seed_plan(db_session, sprint)

        reconcile_once()

        assert stub_queue.enqueued == [req.id]
        assert stub_queue.enqueued_plans == [plan.id]

    def test_plan_db_sweeps_run_when_redis_down(self, db_session, monkeypatch):
        from backend.models.database import TestPlanStatus

        stub = _StubQueueService(available=False)
        monkeypatch.setattr(reconciler_module, "get_queue_service", lambda: stub)
        monkeypatch.setattr(reconciler_module, "reset_queue_service", lambda: None)

        active = _seed_sprint(db_session)
        stale = _seed_plan(
            db_session,
            active,
            status=TestPlanStatus.GENERATING,
            last_heartbeat=_stale_time(),
        )
        finished = _seed_sprint(db_session, active=False)
        orphaned = _seed_plan(db_session, finished)

        reconcile_once()

        stale_row = _reload_plan(db_session, stale.id)
        assert stale_row.status == TestPlanStatus.PENDING
        assert stale_row.retry_count == 1
        orphaned_row = _reload_plan(db_session, orphaned.id)
        assert orphaned_row.status == TestPlanStatus.FAILED
        assert orphaned_row.error == SPRINT_FINISHED_ERROR
        assert stub.enqueued_plans == []


# == Test-execution sweeps (mirroring the requirement/plan sweeps) =====


def _seed_execution(db_session, sprint, status=None, **kwargs):
    """A confirmed requirement + a TestRun + its TestExecution on *sprint*."""
    from backend.tests.test_sprints import _seed_test_execution, _seed_test_run

    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    run = _seed_test_run(db_session, sprint)
    return _seed_test_execution(db_session, run, requirement, status=status, **kwargs)


def _reload_execution(db_session, execution_id):
    from backend.models.database import TestExecution

    db_session.expire_all()
    return db_session.get(TestExecution, execution_id)


class TestTestExecutionSweeps:
    def test_fails_in_progress_executions_on_finished_sprint(self, db_session, stub_queue):
        from backend.models.database import TestExecutionStatus

        sprint = _seed_sprint(db_session, active=False)
        pending = _seed_execution(db_session, sprint)
        running = _seed_execution(
            db_session, sprint, status=TestExecutionStatus.RUNNING, last_heartbeat=_stale_time()
        )

        reconcile_once()

        for execution_id in (pending.id, running.id):
            row = _reload_execution(db_session, execution_id)
            assert row.status == TestExecutionStatus.FAILED
            assert row.error == SPRINT_FINISHED_ERROR
            assert row.last_heartbeat is None
            assert row.retry_count == 0
        assert stub_queue.enqueued_executions == []

    def test_settled_executions_on_finished_sprint_untouched(self, db_session, stub_queue):
        from backend.models.database import TestExecutionStatus

        sprint = _seed_sprint(db_session, active=False)
        seeded = {
            _seed_execution(db_session, sprint, status=status).id: status
            for status in (TestExecutionStatus.COMPLETED, TestExecutionStatus.FAILED)
        }

        reconcile_once()

        for execution_id, status in seeded.items():
            row = _reload_execution(db_session, execution_id)
            assert row.status == status
            assert row.error is None

    def test_stale_running_execution_returns_to_pending_and_reenqueues(
        self, db_session, stub_queue
    ):
        from backend.models.database import TestExecutionStatus

        sprint = _seed_sprint(db_session)
        execution = _seed_execution(
            db_session,
            sprint,
            status=TestExecutionStatus.RUNNING,
            last_heartbeat=_stale_time(),
            job_id="crashed-job",
        )

        reconcile_once()

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.PENDING
        assert row.retry_count == 1
        assert row.last_heartbeat is None
        assert row.job_id == f"execution-job-{execution.id}"
        assert stub_queue.enqueued_executions == [execution.id]

    def test_stale_running_execution_exhausted_marks_failed(self, db_session, stub_queue):
        from backend.models.database import TestExecutionStatus

        sprint = _seed_sprint(db_session)
        execution = _seed_execution(
            db_session,
            sprint,
            status=TestExecutionStatus.RUNNING,
            last_heartbeat=_stale_time(),
            retry_count=2,
        )

        reconcile_once()

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.FAILED
        assert row.retry_count == 3
        assert row.error is not None
        assert stub_queue.enqueued_executions == []

    def test_fresh_running_execution_untouched(self, db_session, stub_queue):
        from backend.models.database import TestExecutionStatus

        sprint = _seed_sprint(db_session)
        execution = _seed_execution(
            db_session,
            sprint,
            status=TestExecutionStatus.RUNNING,
            last_heartbeat=datetime.now(timezone.utc),
        )

        reconcile_once()

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.RUNNING
        assert row.retry_count == 0
        assert stub_queue.enqueued_executions == []

    def test_enqueues_pending_execution_and_persists_job_id(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        execution = _seed_execution(db_session, sprint)

        reconcile_once()

        assert stub_queue.enqueued_executions == [execution.id]
        row = _reload_execution(db_session, execution.id)
        assert row.job_id == f"execution-job-{execution.id}"

    def test_skips_pending_execution_with_live_job(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        execution = _seed_execution(db_session, sprint, job_id="live-execution-job")
        stub_queue.jobs["live-execution-job"] = _StubJob("queued")

        reconcile_once()

        assert stub_queue.enqueued_executions == []
        row = _reload_execution(db_session, execution.id)
        assert row.job_id == "live-execution-job"

    def test_clear_field_none_path_does_not_error(self, db_session, stub_queue):
        """TestExecution has no pending-input field — the setattr skip must
        not raise when clear_field is None (inactive-sprint sweep)."""
        from backend.models.database import TestExecutionStatus

        sprint = _seed_sprint(db_session, active=False)
        execution = _seed_execution(db_session, sprint)

        reconcile_once()  # would raise if the None-clear_field path were broken

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.FAILED

    def test_requirement_plan_and_execution_sweeps_share_a_tick(self, db_session, stub_queue):
        """All three row types are handled in the same reconcile_once call."""
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint)
        plan = _seed_plan(db_session, sprint)
        execution = _seed_execution(db_session, sprint)

        reconcile_once()

        assert stub_queue.enqueued == [req.id]
        assert stub_queue.enqueued_plans == [plan.id]
        assert stub_queue.enqueued_executions == [execution.id]


# == Child rows settled alongside a failed parent ======================


def _seed_execution_with_cases(db_session, sprint, case_statuses, **kwargs):
    """A TestExecution plus one TestCaseExecution per requested status."""
    from backend.tests.test_sprints import (
        _seed_test_case,
        _seed_test_case_execution,
        _seed_test_execution,
        _seed_test_plan,
        _seed_test_run,
    )

    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    plan = _seed_test_plan(db_session, requirement)
    run = _seed_test_run(db_session, sprint)
    execution = _seed_test_execution(db_session, run, requirement, **kwargs)
    for position, status in enumerate(case_statuses):
        case = _seed_test_case(db_session, plan, position=position, title=f"Case {position}")
        _seed_test_case_execution(db_session, execution, case, status=status)
    return execution


def _reload_cases(db_session, execution_id):
    from backend.models.database import TestCaseExecution

    db_session.expire_all()
    return db_session.exec(
        select(TestCaseExecution)
        .where(TestCaseExecution.test_execution_id == execution_id)
        .order_by(TestCaseExecution.id)
    ).all()


class TestChildRowsSettledWithFailedParent:
    """Failing a parent here must not strand its children.

    The reconciler is the one writer that can fail a row without the task
    ever running, so before this it produced the same orphans the task's
    early exits did — a `failed` execution above cases still reading
    "Queued".
    """

    def test_inactive_sprint_sweep_settles_cases(self, db_session, stub_queue):
        from backend.models.database import TestCaseExecutionStatus, TestExecutionStatus

        sprint = _seed_sprint(db_session, active=False)
        execution = _seed_execution_with_cases(
            db_session,
            sprint,
            [TestCaseExecutionStatus.PASSED, TestCaseExecutionStatus.PENDING],
        )

        reconcile_once()

        assert _reload_execution(db_session, execution.id).status == TestExecutionStatus.FAILED
        cases = _reload_cases(db_session, execution.id)
        assert cases[0].status == TestCaseExecutionStatus.PASSED  # keeps its verdict
        assert cases[1].status == TestCaseExecutionStatus.SKIPPED
        assert SPRINT_FINISHED_ERROR in cases[1].error

    def test_exhausted_heartbeat_sweep_settles_cases(self, db_session, stub_queue):
        from backend.models.database import TestCaseExecutionStatus, TestExecutionStatus

        sprint = _seed_sprint(db_session)
        execution = _seed_execution_with_cases(
            db_session,
            sprint,
            [TestCaseExecutionStatus.RUNNING, TestCaseExecutionStatus.PENDING],
            status=TestExecutionStatus.RUNNING,
            last_heartbeat=_stale_time(),
            retry_count=2,
        )

        reconcile_once()

        assert _reload_execution(db_session, execution.id).status == TestExecutionStatus.FAILED
        cases = _reload_cases(db_session, execution.id)
        assert [c.status for c in cases] == [TestCaseExecutionStatus.SKIPPED] * 2
        # The killed worker's in-flight case may have touched the
        # environment; the queued one provably did not.
        assert cases[0].error.startswith("Interrupted before it finished")
        assert cases[1].error.startswith("Not run.")

    def test_repending_sweep_leaves_children_alone(self, db_session, stub_queue):
        """The retry branch must stay resumable — settling here would break it."""
        from backend.models.database import TestCaseExecutionStatus, TestExecutionStatus

        sprint = _seed_sprint(db_session)
        execution = _seed_execution_with_cases(
            db_session,
            sprint,
            [TestCaseExecutionStatus.RUNNING, TestCaseExecutionStatus.PENDING],
            status=TestExecutionStatus.RUNNING,
            last_heartbeat=_stale_time(),
        )

        reconcile_once()

        assert _reload_execution(db_session, execution.id).status == TestExecutionStatus.PENDING
        cases = _reload_cases(db_session, execution.id)
        assert [c.status for c in cases] == [
            TestCaseExecutionStatus.RUNNING,
            TestCaseExecutionStatus.PENDING,
        ]

    def test_exploratory_sessions_settle_the_same_way(self, db_session, stub_queue):
        from backend.models.database import (
            ExploratoryRunStatus,
            ExploratorySession,
            ExploratorySessionStatus,
        )
        from backend.tests.test_sprints import _seed_exploratory_session

        sprint = _seed_sprint(db_session, active=False)
        run = _seed_exploration(db_session, sprint)
        _seed_exploratory_session(db_session, run, position=0)
        _seed_exploratory_session(db_session, run, position=1)

        reconcile_once()

        assert _reload_exploration(db_session, run.id).status == ExploratoryRunStatus.FAILED
        db_session.expire_all()
        sessions = db_session.exec(
            select(ExploratorySession)
            .where(ExploratorySession.exploratory_run_id == run.id)
            .order_by(ExploratorySession.position)
        ).all()
        assert [s.status for s in sessions] == [ExploratorySessionStatus.SKIPPED] * 2
        assert all(SPRINT_FINISHED_ERROR in s.error for s in sessions)


# == Exploratory-run sweeps (mirroring the other three) ================


def _seed_exploration(db_session, sprint, status=None, **kwargs):
    """A confirmed requirement + its ExploratoryRun on *sprint*."""
    from backend.tests.test_sprints import _seed_exploratory_run

    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    if status is not None:
        kwargs["status"] = status
    return _seed_exploratory_run(db_session, sprint, requirement, **kwargs)


def _reload_exploration(db_session, run_id):
    from backend.models.database import ExploratoryRun

    db_session.expire_all()
    return db_session.get(ExploratoryRun, run_id)


class TestExploratoryRunSweeps:
    def test_fails_in_progress_runs_on_finished_sprint(self, db_session, stub_queue):
        from backend.models.database import ExploratoryRunStatus

        sprint = _seed_sprint(db_session, active=False)
        pending = _seed_exploration(db_session, sprint)
        running = _seed_exploration(
            db_session, sprint, status=ExploratoryRunStatus.RUNNING, last_heartbeat=_stale_time()
        )

        reconcile_once()

        for run_id in (pending.id, running.id):
            row = _reload_exploration(db_session, run_id)
            assert row.status == ExploratoryRunStatus.FAILED
            assert row.error == SPRINT_FINISHED_ERROR
            assert row.last_heartbeat is None
        assert stub_queue.enqueued_explorations == []

    def test_settled_runs_on_finished_sprint_untouched(self, db_session, stub_queue):
        from backend.models.database import ExploratoryRunStatus

        sprint = _seed_sprint(db_session, active=False)
        seeded = {
            _seed_exploration(db_session, sprint, status=status).id: status
            for status in (ExploratoryRunStatus.COMPLETED, ExploratoryRunStatus.FAILED)
        }

        reconcile_once()

        for run_id, status in seeded.items():
            row = _reload_exploration(db_session, run_id)
            assert row.status == status
            assert row.error is None

    def test_stale_running_run_returns_to_pending_and_reenqueues(self, db_session, stub_queue):
        from backend.models.database import ExploratoryRunStatus

        sprint = _seed_sprint(db_session)
        run = _seed_exploration(
            db_session,
            sprint,
            status=ExploratoryRunStatus.RUNNING,
            last_heartbeat=_stale_time(),
            job_id="crashed-job",
        )

        reconcile_once()

        row = _reload_exploration(db_session, run.id)
        assert row.status == ExploratoryRunStatus.PENDING
        assert row.retry_count == 1
        assert row.last_heartbeat is None
        assert row.job_id == f"exploration-job-{run.id}"
        assert stub_queue.enqueued_explorations == [run.id]

    def test_stale_running_run_exhausted_marks_failed(self, db_session, stub_queue):
        from backend.models.database import ExploratoryRunStatus

        sprint = _seed_sprint(db_session)
        run = _seed_exploration(
            db_session,
            sprint,
            status=ExploratoryRunStatus.RUNNING,
            last_heartbeat=_stale_time(),
            retry_count=2,
        )

        reconcile_once()

        row = _reload_exploration(db_session, run.id)
        assert row.status == ExploratoryRunStatus.FAILED
        assert "Exploration worker died" in row.error
        assert stub_queue.enqueued_explorations == []

    def test_fresh_running_run_untouched(self, db_session, stub_queue):
        from backend.models.database import ExploratoryRunStatus

        sprint = _seed_sprint(db_session)
        run = _seed_exploration(
            db_session,
            sprint,
            status=ExploratoryRunStatus.RUNNING,
            last_heartbeat=datetime.now(timezone.utc),
        )

        reconcile_once()

        row = _reload_exploration(db_session, run.id)
        assert row.status == ExploratoryRunStatus.RUNNING
        assert row.retry_count == 0

    def test_enqueues_pending_run_and_persists_job_id(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        run = _seed_exploration(db_session, sprint)

        reconcile_once()

        assert stub_queue.enqueued_explorations == [run.id]
        assert _reload_exploration(db_session, run.id).job_id == f"exploration-job-{run.id}"

    def test_skips_pending_run_with_live_job(self, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        _seed_exploration(db_session, sprint, job_id="live-job")
        stub_queue.jobs["live-job"] = _StubJob("queued")

        reconcile_once()

        assert stub_queue.enqueued_explorations == []

    def test_all_four_sweeps_share_a_tick(self, db_session, stub_queue):
        """The fourth row type joins the same parametrized sweep."""
        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint)
        plan = _seed_plan(db_session, sprint)
        execution = _seed_execution(db_session, sprint)
        exploration = _seed_exploration(db_session, sprint)

        reconcile_once()

        assert stub_queue.enqueued == [requirement.id]
        assert stub_queue.enqueued_plans == [plan.id]
        assert stub_queue.enqueued_executions == [execution.id]
        assert stub_queue.enqueued_explorations == [exploration.id]
