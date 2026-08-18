"""Tests for backend/tasks/execute_test.py — the task as a plain function.

The conftest engine monkeypatch makes ``new_session()`` hit the same
in-memory SQLite database as ``db_session``; ``services.llm`` script
functions and ``services.script_runner.run_script`` are monkeypatched — no
Redis, no network, no real subprocess/Playwright.
"""

import json

import pytest
from sqlmodel import select

from backend.config import MAX_AUTO_RETRIES
from backend.models.database import (
    SPRINT_FINISHED_ERROR,
    FindingSeverity,
    FindingType,
    RequirementStatus,
    TestCase,
    TestCaseExecution,
    TestCaseExecutionStatus,
    TestEnvironmentStatus,
    TestExecution,
    TestExecutionStatus,
    TestPlanStatus,
)
from backend.services.llm import LLMError, ScriptDiagnosisResult, TestScriptResult
from backend.services.script_runner import ScriptRunResult
from backend.tasks.execute_test import execute_test_task
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import (
    _seed_test_case,
    _seed_test_case_execution,
    _seed_test_env,
    _seed_test_execution,
    _seed_test_plan,
    _seed_test_run,
)

DEFAULT_ENV_VARS_JSON = json.dumps({"BASE_URL": "https://staging.example.com"})


@pytest.fixture(autouse=True)
def _isolate_readme_resolution(monkeypatch):
    """Keep README resolution deterministic: no disk reads, no GitHub calls."""
    import backend.utils.readme_utils as readme_utils

    async def _no_readme(*args, **kwargs):
        return None

    monkeypatch.setattr(readme_utils, "STORE_OFFLINE", False)
    monkeypatch.setattr(readme_utils, "download_readme", _no_readme)


@pytest.fixture
def llm_stub(monkeypatch):
    """Replace both script LLM entry points with a recording stub.

    ``stub.script_result`` / ``stub.diagnosis_result`` may be their result
    type or an exception to raise.
    """

    class _Stub:
        def __init__(self):
            self.script_result = TestScriptResult(script="print('generated')")
            self.diagnosis_result = ScriptDiagnosisResult(
                classification="script_bug",
                fixed_script="print('fixed')",
                explanation="Wrong selector used.",
            )
            self.generate_calls: list[dict] = []
            self.diagnose_calls: list[dict] = []

        @staticmethod
        def _resolve(result):
            if isinstance(result, Exception):
                raise result
            return result

        def generate_test_script(self, **kwargs):
            self.generate_calls.append(kwargs)
            return self._resolve(self.script_result)

        def diagnose_and_fix_script(self, **kwargs):
            self.diagnose_calls.append(kwargs)
            return self._resolve(self.diagnosis_result)

    stub = _Stub()
    import backend.services.llm as llm_module

    monkeypatch.setattr(llm_module, "generate_test_script", stub.generate_test_script)
    monkeypatch.setattr(llm_module, "diagnose_and_fix_script", stub.diagnose_and_fix_script)
    return stub


@pytest.fixture
def script_runner_stub(monkeypatch):
    """Replace ``script_runner.run_script`` with a recording stub.

    ``stub.results`` is an optional queue popped per call; falls back to
    ``stub.default_result`` (a passing run) once exhausted.
    """

    class _Stub:
        def __init__(self):
            self.default_result = ScriptRunResult(
                exit_code=0, stdout="ok", stderr="", timed_out=False
            )
            self.results: list = []
            self.calls: list[dict] = []

        def run_script(self, script, timeout, extra_env=None):
            self.calls.append({"script": script, "timeout": timeout, "extra_env": extra_env})
            if self.results:
                return self.results.pop(0)
            return self.default_result

    stub = _Stub()
    import backend.services.script_runner as script_runner_module

    monkeypatch.setattr(script_runner_module, "run_script", stub.run_script)
    return stub


def _seed_setup(db_session, *, active=True, file_tree="src/app.py\nsrc/db.py", case_count=1):
    """Sprint (confirmed test env with vars) + requirement + approved plan + cases."""
    sprint = _seed_sprint(db_session, active=active)
    if file_tree is not None:
        sprint.repo.file_tree = file_tree
        db_session.add(sprint.repo)
        db_session.commit()
    _seed_test_env(
        db_session,
        sprint,
        status=TestEnvironmentStatus.CONFIRMED,
        env_vars_json=DEFAULT_ENV_VARS_JSON,
    )
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    plan = _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED)
    cases = [
        _seed_test_case(db_session, plan, position=i, title=f"Case {i}") for i in range(case_count)
    ]
    return sprint, requirement, plan, cases


def _seed_execution(db_session, sprint, requirement, cases, **kwargs):
    run = _seed_test_run(db_session, sprint)
    execution = _seed_test_execution(db_session, run, requirement, **kwargs)
    case_execs = [_seed_test_case_execution(db_session, execution, c) for c in cases]
    return execution, case_execs


def _reload_execution(db_session, execution_id) -> TestExecution:
    db_session.expire_all()
    return db_session.get(TestExecution, execution_id)


def _reload_case_execs(db_session, execution_id) -> list[TestCaseExecution]:
    db_session.expire_all()
    return db_session.exec(
        select(TestCaseExecution)
        .where(TestCaseExecution.test_execution_id == execution_id)
        .order_by(TestCaseExecution.id)
    ).all()


class TestHappyPath:
    def test_all_cases_pass_first_try(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=2)
        execution, case_execs = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.COMPLETED
        assert row.last_heartbeat is None
        assert row.retry_count == 0

        results = _reload_case_execs(db_session, execution.id)
        for result in results:
            assert result.status == TestCaseExecutionStatus.PASSED
            assert result.attempts == 1
            assert result.error is None
            assert result.script_snapshot == "print('generated')"

        db_session.expire_all()
        for case in cases:
            assert db_session.get(TestCase, case.id).script == "print('generated')"


class TestScriptReuse:
    def test_cached_script_skips_generation(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        cases[0].script = "print('cached')"
        db_session.add(cases[0])
        db_session.commit()
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)

        assert llm_stub.generate_calls == []
        assert script_runner_stub.calls[0]["script"] == "print('cached')"
        results = _reload_case_execs(db_session, execution.id)
        assert results[0].script_snapshot == "print('cached')"


class TestSelfHeal:
    def test_script_bug_fix_then_pass(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        script_runner_stub.results = [
            ScriptRunResult(exit_code=1, stdout="", stderr="boom", timed_out=False),
            ScriptRunResult(exit_code=0, stdout="ok", stderr="", timed_out=False),
        ]

        execute_test_task(execution.id)

        results = _reload_case_execs(db_session, execution.id)
        assert results[0].status == TestCaseExecutionStatus.PASSED
        assert results[0].attempts == 2
        assert results[0].script_snapshot == "print('fixed')"
        assert len(llm_stub.diagnose_calls) == 1
        db_session.expire_all()
        assert db_session.get(TestCase, cases[0].id).script == "print('fixed')"


class TestAppBug:
    def test_app_bug_fails_immediately(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        script_runner_stub.default_result = ScriptRunResult(
            exit_code=1, stdout="", stderr="assertion failed", timed_out=False
        )
        llm_stub.diagnosis_result = ScriptDiagnosisResult(
            classification="app_bug", fixed_script=None, explanation="Login genuinely broken."
        )

        execute_test_task(execution.id)

        results = _reload_case_execs(db_session, execution.id)
        assert results[0].status == TestCaseExecutionStatus.FAILED
        assert results[0].attempts == 1
        assert results[0].error == "Login genuinely broken."
        assert results[0].script_snapshot == "print('generated')"
        assert len(script_runner_stub.calls) == 1  # no retry after an app_bug verdict
        db_session.expire_all()
        # Script was correct — it caught a real bug — so it's still cached.
        assert db_session.get(TestCase, cases[0].id).script == "print('generated')"


class TestStructuredFindings:
    """A terminal failure reports itself in the shared finding vocabulary."""

    def _app_bug(self, **overrides) -> ScriptDiagnosisResult:
        fields = {
            "classification": "app_bug",
            "fixed_script": None,
            "explanation": "Login genuinely broken.",
            "finding_severity": "high",
            "finding_title": "Valid credentials are rejected",
            "finding_steps_to_reproduce": "Open /login\nSubmit valid credentials",
            "finding_expected": "The user reaches the dashboard",
            "finding_actual": "A 401 is returned",
        }
        fields.update(overrides)
        return ScriptDiagnosisResult(**fields)

    def _run_app_bug(self, db_session, llm_stub, script_runner_stub, diagnosis):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        script_runner_stub.default_result = ScriptRunResult(
            exit_code=1, stdout="", stderr="assertion failed", timed_out=False
        )
        llm_stub.diagnosis_result = diagnosis
        execute_test_task(execution.id)
        return _reload_case_execs(db_session, execution.id)[0]

    def test_app_bug_persists_the_reported_finding(self, db_session, llm_stub, script_runner_stub):
        result = self._run_app_bug(db_session, llm_stub, script_runner_stub, self._app_bug())

        assert result.finding_type == FindingType.BUG
        assert result.finding_severity == "high"
        assert result.finding_title == "Valid credentials are rejected"
        assert result.finding_steps_to_reproduce == "Open /login\nSubmit valid credentials"
        assert result.finding_expected == "The user reaches the dashboard"
        assert result.finding_actual == "A 401 is returned"
        assert result.environment  # non-empty: names the worker host

    def test_omitted_fields_fall_back_to_the_test_case(
        self, db_session, llm_stub, script_runner_stub
    ):
        """Each fallback is a correct value, not a placeholder — which is why
        a slip is filled in here rather than retrying the whole execution."""
        diagnosis = self._app_bug(
            finding_severity=None,
            finding_title=None,
            finding_steps_to_reproduce=None,
            finding_expected=None,
            finding_actual=None,
        )

        result = self._run_app_bug(db_session, llm_stub, script_runner_stub, diagnosis)

        assert result.finding_title == "Case 0"  # the test case's own title
        assert result.finding_steps_to_reproduce == (
            "Open the login page\nSubmit valid credentials"
        )
        assert result.finding_expected == "User lands on the dashboard."
        assert result.finding_actual == "Login genuinely broken."  # the explanation
        assert result.finding_severity == FindingSeverity.MEDIUM

    def test_out_of_range_severity_normalizes(self, db_session, llm_stub, script_runner_stub):
        """An unknown severity would render as an unstyled badge and sort nowhere."""
        result = self._run_app_bug(
            db_session, llm_stub, script_runner_stub, self._app_bug(finding_severity="critical")
        )

        assert result.finding_severity == FindingSeverity.MEDIUM

    def test_exhausted_self_heal_reports_an_issue_without_an_llm_report(
        self, db_session, llm_stub, script_runner_stub, monkeypatch
    ):
        import backend.tasks.execute_test as execute_test_module

        monkeypatch.setattr(execute_test_module, "MAX_SCRIPT_FIX_ROUNDS", 1)
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        script_runner_stub.default_result = ScriptRunResult(
            exit_code=1, stdout="", stderr="still broken", timed_out=False
        )
        llm_stub.diagnosis_result = ScriptDiagnosisResult(
            classification="script_bug", fixed_script="print('still wrong')", explanation="Bad."
        )

        execute_test_task(execution.id)

        result = _reload_case_execs(db_session, execution.id)[0]
        assert result.finding_type == FindingType.ISSUE
        # Medium regardless: this says nothing about the product, only that
        # nobody managed to look at it.
        assert result.finding_severity == FindingSeverity.MEDIUM
        assert result.finding_title == "Could not verify: Case 0"
        assert result.finding_expected == "User lands on the dashboard."
        assert "never actually exercised" in result.finding_actual
        assert result.environment

    def test_passing_case_carries_no_finding(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)

        result = _reload_case_execs(db_session, execution.id)[0]
        assert result.status == TestCaseExecutionStatus.PASSED
        assert result.finding is None
        assert result.finding_title is None
        # environment travels with the finding rather than the case: it is
        # only reachable through the nested `finding` response, so writing it
        # on a passing case would store what no reader can see.
        assert result.environment is None

    def test_a_restarted_case_that_now_passes_clears_its_old_finding(
        self, db_session, llm_stub, script_runner_stub
    ):
        """A restart reuses the same row, so a fixed bug must stop reporting."""
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, case_execs = _seed_execution(db_session, sprint, requirement, cases)
        case_execs[0].finding_severity = "high"
        case_execs[0].finding_title = "Stale bug from the previous attempt"
        case_execs[0].finding_expected = "old expected"
        db_session.add(case_execs[0])
        db_session.commit()

        execute_test_task(execution.id)

        result = _reload_case_execs(db_session, execution.id)[0]
        assert result.status == TestCaseExecutionStatus.PASSED
        assert result.finding_title is None
        assert result.finding_severity is None
        assert result.finding_expected is None
        assert result.environment is None
        assert result.finding is None


class TestExhaustedSelfHeal:
    def test_script_bug_every_attempt_ends_in_error(
        self, db_session, llm_stub, script_runner_stub, monkeypatch
    ):
        import backend.tasks.execute_test as execute_test_module

        monkeypatch.setattr(execute_test_module, "MAX_SCRIPT_FIX_ROUNDS", 1)
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        script_runner_stub.default_result = ScriptRunResult(
            exit_code=1, stdout="", stderr="still broken", timed_out=False
        )
        # Always diagnosed as a fixable script bug — but the fix cap is 1.
        llm_stub.diagnosis_result = ScriptDiagnosisResult(
            classification="script_bug", fixed_script="print('still wrong')", explanation="Bad."
        )

        execute_test_task(execution.id)

        results = _reload_case_execs(db_session, execution.id)
        assert results[0].status == TestCaseExecutionStatus.ERROR
        assert results[0].attempts == 2  # 1 initial + 1 fix round
        assert results[0].script_snapshot == "print('still wrong')"
        db_session.expire_all()
        # ERROR never updates the cache — still looks broken.
        assert db_session.get(TestCase, cases[0].id).script is None


class TestResumability:
    def test_already_finalized_cases_are_skipped(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=2)
        execution, case_execs = _seed_execution(db_session, sprint, requirement, cases)
        case_execs[0].status = TestCaseExecutionStatus.PASSED
        case_execs[0].attempts = 1
        case_execs[0].script_snapshot = "print('already done')"
        db_session.add(case_execs[0])
        db_session.commit()

        execute_test_task(execution.id)

        assert len(script_runner_stub.calls) == 1  # only the pending case ran
        results = _reload_case_execs(db_session, execution.id)
        assert results[0].script_snapshot == "print('already done')"  # untouched
        assert results[1].status == TestCaseExecutionStatus.PASSED


class TestIdempotencyGuards:
    def test_missing_row_is_noop(self, db_session, llm_stub, script_runner_stub):
        execute_test_task(99999)
        assert llm_stub.generate_calls == []

    @pytest.mark.parametrize("status", [TestExecutionStatus.COMPLETED, TestExecutionStatus.FAILED])
    def test_skips_settled_rows(self, db_session, llm_stub, script_runner_stub, status):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        execution.status = status
        db_session.add(execution)
        db_session.commit()

        execute_test_task(execution.id)

        assert _reload_execution(db_session, execution.id).status == status
        assert llm_stub.generate_calls == []


class TestFinishedSprintGuard:
    def test_inactive_sprint_marks_failed(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session, active=False)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.FAILED
        assert row.error == SPRINT_FINISHED_ERROR
        assert llm_stub.generate_calls == []

    def test_archived_requirement_marks_failed(self, db_session, llm_stub, script_runner_stub):
        """A deleted requirement must never have scripts generated or run."""
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        requirement.archived = True
        db_session.add(requirement)
        db_session.commit()

        execute_test_task(execution.id)

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.FAILED
        assert llm_stub.generate_calls == []


class TestPlanNotApprovedGuard:
    def test_marks_failed(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        plan.status = TestPlanStatus.DRAFT
        db_session.add(plan)
        db_session.commit()
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.FAILED
        assert llm_stub.generate_calls == []


class TestEnvVarsMissingGuard:
    def test_marks_failed_without_touching_llm(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        sprint.test_environment.env_vars_json = None
        db_session.add(sprint.test_environment)
        db_session.commit()
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.FAILED
        assert "not been established" in row.error
        assert llm_stub.generate_calls == []
        results = _reload_case_execs(db_session, execution.id)
        # Never started, and the parent is terminal — so settled, not left
        # reading as "Queued" under a failed run.
        assert results[0].status == TestCaseExecutionStatus.SKIPPED
        assert "not been established" in results[0].error


class TestContextPassedToLLM:
    def test_env_var_names_and_extra_env_reach_every_call(
        self, db_session, llm_stub, script_runner_stub
    ):
        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=2)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)

        for call in llm_stub.generate_calls:
            assert call["env_var_names"] == ["BASE_URL"]
            assert call["name"] == requirement.name
            assert call["file_tree"] == "src/app.py\nsrc/db.py"
        for call in script_runner_stub.calls:
            assert call["extra_env"] == {"BASE_URL": "https://staging.example.com"}


class TestMidFlightStatusChange:
    def test_stops_before_next_case(self, db_session, llm_stub, script_runner_stub, monkeypatch):
        from backend.database import new_session

        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=2)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        original_run_script = script_runner_stub.run_script

        def _flip_status_then_run(script, timeout, extra_env=None):
            with new_session() as other:
                row = other.get(TestExecution, execution.id)
                row.status = TestExecutionStatus.FAILED
                row.error = "flipped mid-run"
                other.add(row)
                other.commit()
            return original_run_script(script, timeout, extra_env=extra_env)

        import backend.services.script_runner as script_runner_module

        monkeypatch.setattr(script_runner_module, "run_script", _flip_status_then_run)

        execute_test_task(execution.id)

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.FAILED
        assert row.error == "flipped mid-run"
        results = _reload_case_execs(db_session, execution.id)
        # The guard is checked before each case starts, not mid-case — the
        # first case (already past its guard when the flip landed) still
        # finishes normally; the second case's guard then sees the flipped
        # status and the loop stops before touching it.
        assert results[0].status == TestCaseExecutionStatus.PASSED
        assert results[1].status == TestCaseExecutionStatus.PENDING


class TestOutputTruncation:
    def test_long_output_truncated(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        script_runner_stub.default_result = ScriptRunResult(
            exit_code=0, stdout="x" * 10000, stderr="", timed_out=False
        )

        execute_test_task(execution.id)

        results = _reload_case_execs(db_session, execution.id)
        assert len(results[0].output) <= 5000


class TestFailureHandling:
    def test_llm_error_returns_execution_to_pending(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        llm_stub.script_result = LLMError("boom")

        execute_test_task(execution.id)

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.PENDING
        assert row.retry_count == 1
        assert row.last_heartbeat is None

        # The case the attempt died inside stays `running`: the parent is
        # going to be re-enqueued, and resumability is derived from exactly
        # this status. Settling it here would be the bug in reverse.
        results = _reload_case_execs(db_session, execution.id)
        assert results[0].status == TestCaseExecutionStatus.RUNNING

    def test_retries_exhausted_marks_failed(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=3)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        execution.retry_count = MAX_AUTO_RETRIES - 1
        db_session.add(execution)
        db_session.commit()
        llm_stub.script_result = LLMError("boom " * 200)

        execute_test_task(execution.id)

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.FAILED
        assert row.retry_count == MAX_AUTO_RETRIES
        assert len(row.error) <= 300

        # Terminal now, so no case may stay in flight — including the one
        # that was already committed as `running` when the job raised.
        results = _reload_case_execs(db_session, execution.id)
        assert [r.status for r in results] == [TestCaseExecutionStatus.SKIPPED] * 3
        # The one that was mid-flight says so: it may have run a script
        # against the live environment, the other two provably did not.
        assert results[0].error.startswith("Interrupted before it finished")
        assert "partially run against the test environment" in results[0].error
        assert all(r.error.startswith("Not run.") for r in results[1:])


class TestUnreachedCasesAreSettled:
    """A terminal execution must leave no case reading as in-flight.

    The case rows have exactly one writer — the loop in the task — so every
    way the loop can stop early used to strand them, showing "Queued" (or a
    live spinner) forever under a run that had already failed.
    """

    @pytest.mark.parametrize(
        "break_setup",
        [
            pytest.param(
                lambda db, sprint, requirement, plan: setattr(sprint, "active", False),
                id="sprint_finished",
            ),
            pytest.param(
                lambda db, sprint, requirement, plan: setattr(requirement, "archived", True),
                id="requirement_deleted",
            ),
            pytest.param(
                lambda db, sprint, requirement, plan: setattr(plan, "status", TestPlanStatus.DRAFT),
                id="plan_unapproved",
            ),
        ],
    )
    def test_every_job_start_guard_settles_its_cases(
        self, db_session, llm_stub, script_runner_stub, break_setup
    ):
        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=2)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        break_setup(db_session, sprint, requirement, plan)
        db_session.add_all([sprint, requirement, plan])
        db_session.commit()

        execute_test_task(execution.id)

        assert _reload_execution(db_session, execution.id).status == TestExecutionStatus.FAILED
        results = _reload_case_execs(db_session, execution.id)
        assert [r.status for r in results] == [TestCaseExecutionStatus.SKIPPED] * 2
        # The parent's reason is repeated on each case, so the row explains
        # itself without the reader having to look up.
        assert all(r.error.startswith("Not run.") for r in results)
        assert llm_stub.generate_calls == []

    def test_finalized_cases_keep_their_verdict(self, db_session, llm_stub, script_runner_stub):
        """Settling must never overwrite a case that already has a result."""
        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=2)
        execution, case_execs = _seed_execution(db_session, sprint, requirement, cases)
        case_execs[0].status = TestCaseExecutionStatus.PASSED
        case_execs[0].error = None
        db_session.add(case_execs[0])
        plan.status = TestPlanStatus.DRAFT
        db_session.add(plan)
        db_session.commit()

        execute_test_task(execution.id)

        results = _reload_case_execs(db_session, execution.id)
        assert results[0].status == TestCaseExecutionStatus.PASSED
        assert results[0].error is None
        assert results[1].status == TestCaseExecutionStatus.SKIPPED

    def test_restart_re_runs_skipped_cases(self, db_session, llm_stub, script_runner_stub):
        """`skipped` is not a finalized status — a restart picks it back up.

        This is why the status is safe to write without a matching reset in
        the restart route: the task's resumability check only skips
        passed/failed/error.
        """
        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=2)
        execution, case_execs = _seed_execution(db_session, sprint, requirement, cases)
        for case_exec in case_execs:
            case_exec.status = TestCaseExecutionStatus.SKIPPED
            case_exec.error = "Not run. Something stopped the last attempt."
            db_session.add(case_exec)
        db_session.commit()

        execute_test_task(execution.id)

        assert _reload_execution(db_session, execution.id).status == TestExecutionStatus.COMPLETED
        results = _reload_case_execs(db_session, execution.id)
        assert [r.status for r in results] == [TestCaseExecutionStatus.PASSED] * 2
        assert all(r.error is None for r in results)


class TestSupersededMidRun:
    """An upstream edit stops the run at the next case boundary.

    Nothing breaks if it carries on — the cases still resolve and the run is
    marked out of date at the end — but each remaining case costs an LLM
    call and a subprocess for a result nobody wants. This replaced a
    route-level guard that blocked the user's edit instead, which could
    never be airtight against a concurrent request anyway.
    """

    def _bump_requirement_elsewhere(self, requirement_id):
        """Commit the cascade from another session, as the route would."""
        from backend.database import new_session
        from backend.models.database import Requirement

        with new_session() as other:
            row = other.get(Requirement, requirement_id)
            row.content_revision += 1
            other.add(row)
            other.commit()

    def test_stops_before_the_next_case_and_says_why(
        self, db_session, llm_stub, script_runner_stub
    ):
        from backend.models.database import SUPERSEDED_ERROR

        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=3)
        execution, case_execs = _seed_execution(
            db_session,
            sprint,
            requirement,
            cases,
            requirement_revision=requirement.content_revision,
            plan_revision=plan.content_revision,
            env_revision=sprint.test_environment.content_revision,
        )
        requirement_id = requirement.id

        import backend.services.llm as llm_module

        original = llm_stub.generate_test_script
        calls = {"n": 0}

        def _bump_after_first(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                self._bump_requirement_elsewhere(requirement_id)
            return original(**kwargs)

        llm_module.generate_test_script = _bump_after_first

        execute_test_task(execution.id)

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.FAILED
        assert row.error == SUPERSEDED_ERROR
        statuses = [c.status for c in row.cases]
        # First case finished; the rest were never started — and must not be
        # left `pending` under a failed parent, since this execution is also
        # outdated and can therefore never be restarted.
        assert statuses[0] == TestCaseExecutionStatus.PASSED
        assert statuses[1:] == [TestCaseExecutionStatus.SKIPPED] * 2
        assert all(c.error.startswith("Not run.") for c in row.cases[1:])
        assert all(SUPERSEDED_ERROR in c.error for c in row.cases[1:])
        # Only one script was generated — the saving this exists for.
        assert calls["n"] == 1

    def test_a_current_run_is_untouched(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=2)
        execution, _ = _seed_execution(
            db_session,
            sprint,
            requirement,
            cases,
            requirement_revision=requirement.content_revision,
            plan_revision=plan.content_revision,
            env_revision=sprint.test_environment.content_revision,
        )

        execute_test_task(execution.id)

        row = _reload_execution(db_session, execution.id)
        assert row.status == TestExecutionStatus.COMPLETED
        assert all(c.status == TestCaseExecutionStatus.PASSED for c in row.cases)


class TestFindingExportWiring:
    """Export fires on the completion path and nowhere else.

    "A run that finished reports its bugs automatically; anything else
    waits for a human" is the whole rule, so both halves are asserted
    rather than assumed.
    """

    @pytest.fixture
    def export_spy(self, monkeypatch):
        calls: list = []

        def _spy(session, parent):
            calls.append(parent.id)
            from backend.services.finding_export import ExportOutcome

            return ExportOutcome()

        import backend.services.finding_export as module

        monkeypatch.setattr(module, "export_findings", _spy)
        return calls

    def test_called_once_when_the_execution_completes(
        self, db_session, llm_stub, script_runner_stub, export_spy
    ):
        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=3)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)

        assert export_spy == [execution.id]

    def test_not_called_when_the_sprint_was_finished(
        self, db_session, llm_stub, script_runner_stub, export_spy
    ):
        sprint, requirement, plan, cases = _seed_setup(db_session, active=False)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)

        assert export_spy == []

    def test_not_called_when_the_plan_is_no_longer_approved(
        self, db_session, llm_stub, script_runner_stub, export_spy
    ):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        plan.status = TestPlanStatus.DRAFT
        db_session.add(plan)
        db_session.commit()
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)

        assert export_spy == []

    def test_not_called_when_env_vars_are_missing(
        self, db_session, llm_stub, script_runner_stub, export_spy
    ):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        sprint.test_environment.env_vars_json = None
        db_session.add(sprint.test_environment)
        db_session.commit()
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)

        assert export_spy == []

    def test_not_called_when_the_execution_is_superseded_mid_run(
        self, db_session, llm_stub, script_runner_stub, export_spy
    ):
        """An upstream edit leaves a finding set that is incomplete and
        known to be — the run page's button is where a human decides."""
        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=2)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        requirement.content_revision += 1
        db_session.add(requirement)
        db_session.commit()

        execute_test_task(execution.id)

        assert _reload_execution(db_session, execution.id).status == TestExecutionStatus.FAILED
        assert export_spy == []

    def test_not_called_on_a_terminal_record_failure(
        self, db_session, llm_stub, script_runner_stub, export_spy
    ):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(
            db_session, sprint, requirement, cases, retry_count=MAX_AUTO_RETRIES - 1
        )
        llm_stub.script_result = LLMError("model down")

        execute_test_task(execution.id)

        assert _reload_execution(db_session, execution.id).status == TestExecutionStatus.FAILED
        assert export_spy == []

    def test_not_called_on_a_stale_job(self, db_session, llm_stub, script_runner_stub, export_spy):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(
            db_session, sprint, requirement, cases, status=TestExecutionStatus.COMPLETED
        )

        execute_test_task(execution.id)

        assert export_spy == []

    def test_a_raising_exporter_cannot_fail_the_run(
        self, db_session, llm_stub, script_runner_stub, monkeypatch
    ):
        """`export_findings` guarantees it never raises; the task guards
        that guarantee anyway. A job in RQ's failed registry for a run
        that plainly succeeded is exactly the contradiction someone
        debugging spends an hour on."""
        import backend.services.finding_export as module

        def _boom(session, parent):
            raise RuntimeError("tracker exploded")

        monkeypatch.setattr(module, "export_findings", _boom)
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)  # must not raise

        assert _reload_execution(db_session, execution.id).status == TestExecutionStatus.COMPLETED


class TestDefectGroupingWiring:
    """Grouping fires on the completion path, and always before export.

    The order is load-bearing rather than incidental: export files one
    ticket per defect group, so a grouping that ran afterwards would file
    the tickets the grouping existed to prevent.
    """

    @pytest.fixture
    def order(self, monkeypatch):
        """Both calls, in the order the task made them."""
        calls: list = []

        import backend.services.finding_export as export_module
        import backend.services.finding_grouping as grouping_module

        def _group(session, parent):
            calls.append(("group", parent.id))

        def _export(session, parent):
            calls.append(("export", parent.id))
            return export_module.ExportOutcome()

        monkeypatch.setattr(grouping_module, "assign_defect_groups", _group)
        monkeypatch.setattr(export_module, "export_findings", _export)
        return calls

    def test_grouping_precedes_export_on_the_completion_path(
        self, db_session, llm_stub, script_runner_stub, order
    ):
        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=2)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)

        assert order == [("group", execution.id), ("export", execution.id)]

    def test_not_called_when_the_execution_is_superseded_mid_run(
        self, db_session, llm_stub, script_runner_stub, order
    ):
        """`_fail_execution`'s chokepoint — an incomplete finding set is
        not what the sprint's defect list should be built from."""
        sprint, requirement, plan, cases = _seed_setup(db_session, case_count=2)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        requirement.content_revision += 1
        db_session.add(requirement)
        db_session.commit()

        execute_test_task(execution.id)

        assert order == []

    def test_not_called_on_a_terminal_record_failure(
        self, db_session, llm_stub, script_runner_stub, order
    ):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(
            db_session, sprint, requirement, cases, retry_count=MAX_AUTO_RETRIES - 1
        )
        llm_stub.script_result = LLMError("model down")

        execute_test_task(execution.id)

        assert _reload_execution(db_session, execution.id).status == TestExecutionStatus.FAILED
        assert order == []

    def test_a_raising_grouping_pass_cannot_fail_the_run_or_stop_the_export(
        self, db_session, llm_stub, script_runner_stub, monkeypatch
    ):
        """`assign_defect_groups` never raises; the task guards it anyway,
        and the export still runs — an ungrouped finding is still a bug
        worth filing."""
        import backend.services.finding_export as export_module
        import backend.services.finding_grouping as grouping_module

        exported: list = []

        def _boom(session, parent):
            raise RuntimeError("grouping exploded")

        monkeypatch.setattr(grouping_module, "assign_defect_groups", _boom)
        monkeypatch.setattr(
            export_module,
            "export_findings",
            lambda session, parent: exported.append(parent.id) or export_module.ExportOutcome(),
        )
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)

        execute_test_task(execution.id)  # must not raise

        assert _reload_execution(db_session, execution.id).status == TestExecutionStatus.COMPLETED
        assert exported == [execution.id]


class TestScriptRevisionStamps:
    """A cached script records what it was written against.

    Stamped only where the script is cached, so "has a cached script" keeps
    meaning "ran to a verdict" and the stamps cannot drift from the text
    they describe.  `services/cicd_eligibility.py` reads them.
    """

    def _stamps(self, db_session, case):
        db_session.expire_all()
        row = db_session.get(TestCase, case.id)
        return (
            row.script_requirement_revision,
            row.script_plan_revision,
            row.script_env_revision,
        )

    def test_a_passing_case_stamps_all_three_revisions(
        self, db_session, llm_stub, script_runner_stub
    ):
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        expected = (
            requirement.content_revision,
            plan.content_revision,
            sprint.test_environment.content_revision,
        )

        execute_test_task(execution.id)

        assert self._stamps(db_session, cases[0]) == expected

    def test_an_app_bug_case_stamps_them_too(self, db_session, llm_stub, script_runner_stub):
        """The script was right — it caught a real bug — so it is cached and stamped."""
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        script_runner_stub.default_result = ScriptRunResult(
            exit_code=1, stdout="", stderr="assertion failed", timed_out=False
        )
        llm_stub.diagnosis_result = ScriptDiagnosisResult(
            classification="app_bug", fixed_script=None, explanation="Login genuinely broken."
        )

        execute_test_task(execution.id)

        results = _reload_case_execs(db_session, execution.id)
        assert results[0].status == TestCaseExecutionStatus.FAILED
        assert self._stamps(db_session, cases[0]) == (
            requirement.content_revision,
            plan.content_revision,
            sprint.test_environment.content_revision,
        )

    def test_an_errored_case_stamps_nothing(self, db_session, llm_stub, script_runner_stub):
        """Self-heal exhausted — nothing is cached, so there is nothing to describe."""
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        script_runner_stub.default_result = ScriptRunResult(
            exit_code=1, stdout="", stderr="still broken", timed_out=False
        )
        llm_stub.diagnosis_result = ScriptDiagnosisResult(
            classification="script_bug", fixed_script="print('still wrong')", explanation="Bad."
        )

        execute_test_task(execution.id)

        results = _reload_case_execs(db_session, execution.id)
        assert results[0].status == TestCaseExecutionStatus.ERROR
        assert self._stamps(db_session, cases[0]) == (None, None, None)

    def test_a_rerun_after_an_edit_restamps_the_new_revisions(
        self, db_session, llm_stub, script_runner_stub
    ):
        """The stamp follows the script, so an edited requirement is reflected."""
        sprint, requirement, plan, cases = _seed_setup(db_session)
        execution, _ = _seed_execution(db_session, sprint, requirement, cases)
        execute_test_task(execution.id)

        requirement.content_revision += 1
        db_session.add(requirement)
        db_session.commit()
        # The new run must copy the *new* revision, or the task correctly
        # refuses to run it as superseded.
        second, _ = _seed_execution(
            db_session,
            sprint,
            requirement,
            cases,
            requirement_revision=requirement.content_revision,
            plan_revision=plan.content_revision,
            env_revision=sprint.test_environment.content_revision,
        )
        execute_test_task(second.id)

        assert self._stamps(db_session, cases[0])[0] == requirement.content_revision
