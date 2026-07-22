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


def _seed_execution(db_session, sprint, requirement, cases):
    run = _seed_test_run(db_session, sprint)
    execution = _seed_test_execution(db_session, run, requirement)
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
        assert results[0].status == TestCaseExecutionStatus.PENDING  # untouched


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

    def test_retries_exhausted_marks_failed(self, db_session, llm_stub, script_runner_stub):
        sprint, requirement, plan, cases = _seed_setup(db_session)
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
