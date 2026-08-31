"""Tests for backend/services/finalization.py — the shared retry protocol.

The module is exercised transitively by ``test_execute_test.py``,
``test_explore_requirement.py`` and ``test_reconciler.py``, but nothing
asserted its own contract, so a regression in a module all four run types
share surfaced three files away from its cause.  This suite pins the
contract directly: what ``record_failure`` does under and at the cap, what
``fail_row`` does instead, and the three properties
``abandon_unreached_children`` is relied on for.

Everything below drives the exported specs rather than ad-hoc ones, so the
assertions describe the behaviour the tasks actually get.
"""

import pytest

import backend.services.finalization as finalization
from backend.config import MAX_AUTO_RETRIES
from backend.models.database import (
    ExploratoryRun,
    ExploratoryRunStatus,
    ExploratorySession,
    ExploratorySessionStatus,
    RequirementStatus,
    TestCaseExecution,
    TestCaseExecutionStatus,
    TestExecution,
    TestExecutionStatus,
)
from backend.services.finalization import (
    CICD_EXPORT_SPEC,
    ERROR_SUMMARY_MAX_CHARS,
    EXPLORATORY_RUN_SPEC,
    EXPLORATORY_SESSION_SPEC,
    TEST_CASE_SPEC,
    TEST_EXECUTION_SPEC,
    abandon_unreached_children,
    fail_row,
    record_failure,
)
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import (
    _seed_exploratory_run,
    _seed_exploratory_session,
    _seed_test_case,
    _seed_test_case_execution,
    _seed_test_execution,
    _seed_test_plan,
    _seed_test_run,
)

# ── Fixtures ──────────────────────────────────────────────────────────


def _execution_with_cases(db_session, *, status=TestExecutionStatus.RUNNING):
    """A running TestExecution over one finished and two unreached cases."""
    sprint = _seed_sprint(db_session)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    plan = _seed_test_plan(db_session, requirement)
    run = _seed_test_run(db_session, sprint)
    execution = _seed_test_execution(db_session, run, requirement, status=status)
    done = _seed_test_case_execution(
        db_session,
        execution,
        _seed_test_case(db_session, plan, position=0, title="Done"),
        status=TestCaseExecutionStatus.PASSED,
    )
    queued = _seed_test_case_execution(
        db_session,
        execution,
        _seed_test_case(db_session, plan, position=1, title="Queued"),
        status=TestCaseExecutionStatus.PENDING,
    )
    started = _seed_test_case_execution(
        db_session,
        execution,
        _seed_test_case(db_session, plan, position=2, title="Started"),
        status=TestCaseExecutionStatus.RUNNING,
    )
    return execution, done, queued, started


def _run_with_sessions(db_session):
    """A running ExploratoryRun over one completed and one pending session."""
    sprint = _seed_sprint(db_session)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    run = _seed_exploratory_run(
        db_session, sprint, requirement, status=ExploratoryRunStatus.RUNNING
    )
    done = _seed_exploratory_session(
        db_session, run, position=0, status=ExploratorySessionStatus.COMPLETED
    )
    queued = _seed_exploratory_session(
        db_session, run, position=1, status=ExploratorySessionStatus.PENDING
    )
    return run, done, queued


def _statuses(db_session, model, ids):
    db_session.expire_all()
    return [db_session.get(model, row_id).status for row_id in ids]


# ── record_failure ────────────────────────────────────────────────────


class TestRecordFailure:
    def test_under_the_cap_re_pends_and_leaves_children_alone(self, db_session):
        """A row going back to pending is resumed exactly where it stopped."""
        execution, done, queued, started = _execution_with_cases(db_session)

        record_failure(db_session, TEST_EXECUTION_SPEC, execution.id, RuntimeError("boom"))

        db_session.expire_all()
        row = db_session.get(TestExecution, execution.id)
        assert row.status == TestExecutionStatus.PENDING
        assert row.retry_count == 1
        assert row.last_heartbeat is None
        assert row.error is None
        assert _statuses(db_session, TestCaseExecution, [done.id, queued.id, started.id]) == [
            TestCaseExecutionStatus.PASSED,
            TestCaseExecutionStatus.PENDING,
            TestCaseExecutionStatus.RUNNING,
        ]

    def test_at_the_cap_fails_the_row_and_settles_children(self, db_session):
        execution, done, queued, started = _execution_with_cases(db_session)
        execution.retry_count = MAX_AUTO_RETRIES - 1
        db_session.add(execution)
        db_session.commit()

        record_failure(db_session, TEST_EXECUTION_SPEC, execution.id, RuntimeError("boom"))

        db_session.expire_all()
        row = db_session.get(TestExecution, execution.id)
        assert row.status == TestExecutionStatus.FAILED
        assert row.retry_count == MAX_AUTO_RETRIES
        assert row.error == "boom"
        assert _statuses(db_session, TestCaseExecution, [done.id, queued.id, started.id]) == [
            TestCaseExecutionStatus.PASSED,
            TestCaseExecutionStatus.SKIPPED,
            TestCaseExecutionStatus.SKIPPED,
        ]

    def test_truncates_the_error_text(self, db_session):
        execution, *_ = _execution_with_cases(db_session)
        execution.retry_count = MAX_AUTO_RETRIES - 1
        db_session.add(execution)
        db_session.commit()

        record_failure(db_session, TEST_EXECUTION_SPEC, execution.id, RuntimeError("x" * 1000))

        db_session.expire_all()
        assert len(db_session.get(TestExecution, execution.id).error) == ERROR_SUMMARY_MAX_CHARS

    def test_a_missing_row_is_a_no_op(self, db_session):
        record_failure(db_session, TEST_EXECUTION_SPEC, 9999, RuntimeError("boom"))

    def test_a_childless_spec_fails_without_a_type_error(self, db_session):
        """CICD_EXPORT_SPEC has no children by design, not by omission."""
        from backend.models.database import CicdExport, CicdExportStatus, CicdProvider

        sprint = _seed_sprint(db_session)
        export = CicdExport(
            sprint_id=sprint.id,
            provider=CicdProvider.GITHUB_ACTIONS,
            status=CicdExportStatus.RUNNING,
            retry_count=MAX_AUTO_RETRIES - 1,
            selected_case_ids_json="[]",
        )
        db_session.add(export)
        db_session.commit()
        db_session.refresh(export)

        record_failure(db_session, CICD_EXPORT_SPEC, export.id, RuntimeError("boom"))

        db_session.expire_all()
        assert db_session.get(CicdExport, export.id).status == CicdExportStatus.FAILED


# ── fail_row ──────────────────────────────────────────────────────────


class TestFailRow:
    def test_settles_children_and_spends_no_retry(self, db_session):
        execution, done, queued, started = _execution_with_cases(db_session)

        fail_row(db_session, TEST_EXECUTION_SPEC, execution, "Superseded by an edit.")

        db_session.expire_all()
        row = db_session.get(TestExecution, execution.id)
        assert row.status == TestExecutionStatus.FAILED
        assert row.retry_count == 0
        assert row.error == "Superseded by an edit."
        assert row.last_heartbeat is None
        assert _statuses(db_session, TestCaseExecution, [done.id, queued.id, started.id]) == [
            TestCaseExecutionStatus.PASSED,
            TestCaseExecutionStatus.SKIPPED,
            TestCaseExecutionStatus.SKIPPED,
        ]

    def test_settles_the_other_child_type_too(self, db_session):
        run, done, queued = _run_with_sessions(db_session)

        fail_row(db_session, EXPLORATORY_RUN_SPEC, run, "Superseded by an edit.")

        db_session.expire_all()
        assert db_session.get(ExploratoryRun, run.id).status == ExploratoryRunStatus.FAILED
        assert _statuses(db_session, ExploratorySession, [done.id, queued.id]) == [
            ExploratorySessionStatus.COMPLETED,
            ExploratorySessionStatus.SKIPPED,
        ]


# ── abandon_unreached_children ────────────────────────────────────────


class TestAbandonUnreachedChildren:
    def test_splits_the_error_text_by_prior_status(self, db_session):
        """A pending child provably never ran; a running one may have."""
        execution, _done, queued, started = _execution_with_cases(db_session)

        abandon_unreached_children(db_session, TEST_CASE_SPEC, execution.id, "Sprint finished.")
        db_session.commit()

        db_session.expire_all()
        never = db_session.get(TestCaseExecution, queued.id)
        cut_off = db_session.get(TestCaseExecution, started.id)
        assert never.error.startswith("Not run.")
        assert "Sprint finished." in never.error
        assert cut_off.error.startswith("Interrupted before it finished")
        assert "partially run against the test environment" in cut_off.error

    def test_is_idempotent(self, db_session):
        execution, _done, queued, started = _execution_with_cases(db_session)

        abandon_unreached_children(db_session, TEST_CASE_SPEC, execution.id, "First.")
        db_session.commit()
        abandon_unreached_children(db_session, TEST_CASE_SPEC, execution.id, "Second.")
        db_session.commit()

        db_session.expire_all()
        for row_id in (queued.id, started.id):
            row = db_session.get(TestCaseExecution, row_id)
            assert row.status == TestCaseExecutionStatus.SKIPPED
            assert "First." in row.error  # the second pass matched nothing

    def test_leaves_terminal_children_untouched(self, db_session):
        execution, done, *_ = _execution_with_cases(db_session)

        abandon_unreached_children(db_session, TEST_CASE_SPEC, execution.id, "Sprint finished.")
        db_session.commit()

        db_session.expire_all()
        row = db_session.get(TestCaseExecution, done.id)
        assert row.status == TestCaseExecutionStatus.PASSED
        assert row.error is None

    def test_touches_no_other_parents_children(self, db_session):
        _first, _done, queued, _started = _execution_with_cases(db_session)
        second, _done2, other_queued, _started2 = _execution_with_cases(db_session)

        abandon_unreached_children(db_session, TEST_CASE_SPEC, second.id, "Sprint finished.")
        db_session.commit()

        db_session.expire_all()
        assert (
            db_session.get(TestCaseExecution, queued.id).status == TestCaseExecutionStatus.PENDING
        )
        assert (
            db_session.get(TestCaseExecution, other_queued.id).status
            == TestCaseExecutionStatus.SKIPPED
        )

    def test_a_none_parent_id_is_a_no_op(self, db_session):
        abandon_unreached_children(db_session, EXPLORATORY_SESSION_SPEC, None, "reason")


class TestMultipleChildSpecs:
    """A parent may drive more than one kind of child row.

    No shipped parent does yet — a nonfunctional run walks targets *and*
    load profiles — so the loop is pinned through a spy rather than through
    a contrived foreign-key collision: what matters is that every spec in
    the tuple is applied, once, on the failing branch only.
    """

    @staticmethod
    def _spy(monkeypatch):
        calls: list[tuple] = []
        monkeypatch.setattr(
            finalization,
            "abandon_unreached_children",
            lambda session, spec, parent_id, reason: calls.append((spec, parent_id, reason)),
        )
        return calls

    def _two_spec_row_spec(self):
        return finalization.RowSpec(
            model=TestExecution,
            label="Two-child run",
            pending_status=TestExecutionStatus.PENDING,
            failed_status=TestExecutionStatus.FAILED,
            child_specs=(TEST_CASE_SPEC, EXPLORATORY_SESSION_SPEC),
        )

    def test_fail_row_settles_both_child_types(self, db_session, monkeypatch):
        calls = self._spy(monkeypatch)
        execution, *_ = _execution_with_cases(db_session)

        fail_row(db_session, self._two_spec_row_spec(), execution, "Superseded.")

        assert [spec for spec, _, _ in calls] == [TEST_CASE_SPEC, EXPLORATORY_SESSION_SPEC]
        assert {parent_id for _, parent_id, _ in calls} == {execution.id}

    def test_record_failure_settles_both_only_at_the_cap(self, db_session, monkeypatch):
        calls = self._spy(monkeypatch)
        spec = self._two_spec_row_spec()
        execution, *_ = _execution_with_cases(db_session)

        record_failure(db_session, spec, execution.id, RuntimeError("boom"))
        assert calls == []  # under the cap the next attempt resumes there

        db_session.expire_all()
        row = db_session.get(TestExecution, execution.id)
        row.retry_count = MAX_AUTO_RETRIES - 1
        db_session.add(row)
        db_session.commit()

        record_failure(db_session, spec, execution.id, RuntimeError("boom"))
        assert [child_spec for child_spec, _, _ in calls] == [
            TEST_CASE_SPEC,
            EXPLORATORY_SESSION_SPEC,
        ]


@pytest.mark.parametrize(
    "spec",
    [TEST_EXECUTION_SPEC, EXPLORATORY_RUN_SPEC, CICD_EXPORT_SPEC],
    ids=lambda spec: spec.label,
)
def test_every_spec_names_distinct_pending_and_failed_statuses(spec):
    assert spec.pending_status != spec.failed_status
