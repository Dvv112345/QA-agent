"""Test-execution task, executed by the RQ worker.

One job per requirement (``TestExecution``): walks that requirement's
approved test cases in order, reusing a cached script per case or
generating one, executing it in a subprocess, and self-healing script bugs
via the LLM. Job args are the test-execution id only — everything else is
read fresh from the database, which makes every enqueue idempotent and
reconciler-safe. Resumability is derived entirely from each
``TestCaseExecution``'s own status (no ``pending_feedback``-equivalent
field needed) — rows already finalized (``PASSED``/``FAILED``/``ERROR``)
are skipped on a retry.

Must not import from ``backend.services.queue`` or ``backend.worker``
(circular-import rule).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from sqlmodel import Session, select

from backend.config import (
    MAX_AUTO_RETRIES,
    MAX_SCRIPT_FIX_ROUNDS,
    SCRIPT_EXECUTION_TIMEOUT,
    TEST_PLAN_FILE_MAX_CHARS,
)
from backend.database import new_session
from backend.models.database import (
    SPRINT_FINISHED_ERROR,
    FindingSeverity,
    TestCaseExecution,
    TestCaseExecutionStatus,
    TestExecution,
    TestExecutionStatus,
    TestPlanStatus,
)
from backend.services import llm, script_runner
from backend.services.llm_prompts import TestCaseLike
from backend.utils import environment_utils, github_utils
from backend.utils.crypto import decrypt_token
from backend.utils.readme_utils import resolve_readme

logger = logging.getLogger(__name__)

# Cap for the user-facing error summary stored on a failed row.
_ERROR_SUMMARY_MAX_CHARS = 300

# Cap for stored per-case output/error text (combined stdout+stderr, or a
# diagnosis explanation) — mirrors the _ERROR_SUMMARY_MAX_CHARS precedent.
_OUTPUT_MAX_CHARS = 5000

_FILE_TRUNCATION_MARKER = "\n… (truncated)"

# Should be unreachable via normal flow — guarded per this codebase's
# convention of never trusting a supposedly-impossible state blindly.
_PLAN_NOT_APPROVED_ERROR = "Test plan is no longer approved."
_ENV_VARS_MISSING_ERROR = "Test environment access variables have not been established."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _record_failure(session: Session, test_execution_id: int, exc: Exception) -> None:
    """Count the failure and either re-queue the row or mark it failed.

    A case already finalized before the exception stays finalized — the
    next attempt resumes from the first non-finalized case.
    """
    session.rollback()
    execution = session.get(TestExecution, test_execution_id)
    if execution is None:
        return

    execution.retry_count += 1
    if execution.retry_count >= MAX_AUTO_RETRIES:
        execution.status = TestExecutionStatus.FAILED
        execution.error = str(exc)[:_ERROR_SUMMARY_MAX_CHARS]
    else:
        # Back to pending — the reconciler re-enqueues it.
        execution.status = TestExecutionStatus.PENDING
    execution.last_heartbeat = None
    execution.updated_at = _now()
    session.add(execution)
    session.commit()


def _build_read_file(
    file_tree: str, owner: str, repo: str, token: str | None
) -> Callable[[str], str]:
    """Executor for the LLM's read_file tool: path-validated, truncating,
    never raising — errors go back to the model as strings it can react to.

    Duplicated from ``tasks/generate_test_plan.py`` rather than shared
    (Decision 4) — one file per task type, self-contained.
    """
    allowed_paths = set(file_tree.splitlines())

    def read_file(path: str) -> str:
        requested = (path or "").strip().lstrip("/")
        if requested not in allowed_paths:
            return f"ERROR: could not read '{requested}': path is not in the repository file tree."
        try:
            content = asyncio.run(github_utils.fetch_file(owner, repo, requested, token))
        except github_utils.GitHubError as exc:
            return f"ERROR: could not read '{requested}': {exc}"
        if content is None:
            return f"ERROR: could not read '{requested}': file not found."
        if len(content) > TEST_PLAN_FILE_MAX_CHARS:
            content = content[:TEST_PLAN_FILE_MAX_CHARS] + _FILE_TRUNCATION_MARKER
        return content

    return read_file


# Cap for the finding title — a one-liner, held to the same bound as the
# other short user-facing summaries.
_FINDING_TITLE_MAX_CHARS = 300


def _bug_finding(diagnosis: llm.ScriptDiagnosisResult, test_case: TestCaseLike) -> dict[str, str]:
    """Normalize an app_bug diagnosis into the stored finding fields.

    Every fallback is a genuinely correct value rather than a placeholder:
    the test case *is* the reproduction steps and the expected result, and
    the diagnosis explanation *is* an account of what happened.  That is why
    a missing field is filled here instead of raising — raising would retry
    the entire TestExecution over a formatting slip in a report whose
    substance already arrived in ``explanation``.
    """
    severity = diagnosis.finding_severity
    if severity not in {member.value for member in FindingSeverity}:
        # An unknown severity is worse than an assumed one: it would render
        # as an unstyled badge and sort nowhere.
        severity = FindingSeverity.MEDIUM
    return {
        "finding_severity": severity,
        "finding_title": (diagnosis.finding_title or test_case.title)[:_FINDING_TITLE_MAX_CHARS],
        "finding_steps_to_reproduce": (diagnosis.finding_steps_to_reproduce or test_case.steps)[
            :_OUTPUT_MAX_CHARS
        ],
        "finding_expected": (diagnosis.finding_expected or test_case.expected_result)[
            :_OUTPUT_MAX_CHARS
        ],
        "finding_actual": (diagnosis.finding_actual or diagnosis.explanation)[:_OUTPUT_MAX_CHARS],
    }


def _issue_finding(test_case: TestCaseLike, attempts: int) -> dict[str, str]:
    """Build the finding for a case whose script could never be made to run.

    No LLM call: the last diagnosis said ``script_bug``, so there is no
    verdict about the product to report.  Severity is fixed at medium
    because this says nothing about the product — the requirement may be
    perfectly well met, and nobody has been able to look.
    """
    return {
        "finding_severity": FindingSeverity.MEDIUM,
        "finding_title": f"Could not verify: {test_case.title}"[:_FINDING_TITLE_MAX_CHARS],
        "finding_steps_to_reproduce": test_case.steps[:_OUTPUT_MAX_CHARS],
        "finding_expected": test_case.expected_result[:_OUTPUT_MAX_CHARS],
        "finding_actual": (
            f"The generated test script could not be made to run correctly after "
            f"{attempts} attempts, so this test case was never actually exercised. "
            "The failure looked like a problem with the script rather than the "
            "application every time."
        ),
    }


# Cleared on a passing case. A restarted execution reuses its
# TestCaseExecution rows, so a fixed bug would otherwise keep reporting
# itself from the previous attempt.
_NO_FINDING = {
    "finding_severity": None,
    "finding_title": None,
    "finding_steps_to_reproduce": None,
    "finding_expected": None,
    "finding_actual": None,
}


def _fail_execution(session: Session, execution: TestExecution, error: str) -> None:
    execution.status = TestExecutionStatus.FAILED
    execution.error = error
    execution.last_heartbeat = None
    execution.updated_at = _now()
    session.add(execution)
    session.commit()


def execute_test_task(test_execution_id: int) -> None:
    """Run (or resume) every non-finalized test case for one requirement."""
    with new_session() as session:
        execution = session.get(TestExecution, test_execution_id)
        if execution is None:
            logger.info("Test execution %d no longer exists — skipping", test_execution_id)
            return
        if execution.status not in (TestExecutionStatus.PENDING, TestExecutionStatus.RUNNING):
            logger.info(
                "Test execution %d is '%s' — skipping stale job",
                test_execution_id,
                execution.status,
            )
            return

        requirement = execution.requirement
        sprint = requirement.sprint if requirement is not None else None
        if sprint is None or not sprint.active:
            _fail_execution(session, execution, SPRINT_FINISHED_ERROR)
            logger.info("Test execution %d: sprint inactive — marked failed", test_execution_id)
            return

        plan = requirement.test_plan
        if plan is None or plan.status != TestPlanStatus.APPROVED:
            _fail_execution(session, execution, _PLAN_NOT_APPROVED_ERROR)
            logger.warning(
                "Test execution %d: plan no longer approved — marked failed", test_execution_id
            )
            return

        # No LLM call happens here at all — env vars are generated exactly
        # once, synchronously, inside the test-environment stage. This is a
        # pure read with a defensive guard (should be unreachable: reaching
        # this task already implies the test environment was confirmed).
        test_env = sprint.test_environment
        env_vars = test_env.env_vars if test_env else None
        if not env_vars:
            _fail_execution(session, execution, _ENV_VARS_MISSING_ERROR)
            logger.warning("Test execution %d: env vars missing — marked failed", test_execution_id)
            return

        execution.status = TestExecutionStatus.RUNNING
        execution.last_heartbeat = _now()
        execution.updated_at = _now()
        session.add(execution)
        session.commit()

        try:
            readme = asyncio.run(resolve_readme(sprint))
            file_tree = sprint.repo.file_tree if sprint.repo else None
            env_var_names = list(env_vars.keys())

            read_file: Callable[[str], str] | None = None
            if file_tree and sprint.repo:
                owner, repo_name = github_utils.parse_github_url(sprint.repo.github_link)
                token = (
                    decrypt_token(sprint.repo.github_token) if sprint.repo.github_token else None
                )
                read_file = _build_read_file(file_tree, owner, repo_name, token)

            def on_round() -> None:
                # Heartbeat between LLM rounds and subprocess runs so a live
                # loop is never swept as a crashed worker.
                execution.last_heartbeat = _now()
                session.add(execution)
                session.commit()

            on_round()

            case_executions = session.exec(
                select(TestCaseExecution)
                .where(TestCaseExecution.test_execution_id == test_execution_id)
                .order_by(TestCaseExecution.id)
            ).all()

            for case_exec in case_executions:
                if case_exec.status in (
                    TestCaseExecutionStatus.PASSED,
                    TestCaseExecutionStatus.FAILED,
                    TestCaseExecutionStatus.ERROR,
                ):
                    continue  # already finalized — resumability

                with session.no_autoflush:
                    current_status = session.exec(
                        select(TestExecution.status).where(TestExecution.id == test_execution_id)
                    ).one_or_none()
                if current_status != TestExecutionStatus.RUNNING:
                    logger.info(
                        "Test execution %d changed to '%s' mid-run — stopping before case %d",
                        test_execution_id,
                        current_status,
                        case_exec.id,
                    )
                    session.rollback()
                    return

                case_exec.status = TestCaseExecutionStatus.RUNNING
                case_exec.updated_at = _now()
                session.add(case_exec)
                session.commit()

                test_case = case_exec.test_case
                case_like = TestCaseLike(
                    title=test_case.title,
                    preconditions=test_case.preconditions,
                    steps=test_case.steps,
                    expected_result=test_case.expected_result,
                    case_type=test_case.case_type,
                    priority=test_case.priority,
                )

                if test_case.script:
                    script = test_case.script
                else:
                    script_result = llm.generate_test_script(
                        name=requirement.name,
                        description=requirement.description,
                        test_case=case_like,
                        env_var_names=env_var_names,
                        readme=readme,
                        file_tree=file_tree,
                        read_file=read_file,
                        on_round=on_round,
                    )
                    script = script_result.script

                attempts = 0
                final_status: str | None = None
                final_output = ""
                final_error: str | None = None
                finding: dict[str, str | None] = dict(_NO_FINDING)
                while True:
                    attempts += 1
                    run_result = script_runner.run_script(
                        script, SCRIPT_EXECUTION_TIMEOUT, extra_env=env_vars
                    )
                    on_round()

                    if run_result.passed:
                        final_status = TestCaseExecutionStatus.PASSED
                        final_output = f"{run_result.stdout}\n{run_result.stderr}".strip()
                        break

                    if attempts > MAX_SCRIPT_FIX_ROUNDS:
                        final_status = TestCaseExecutionStatus.ERROR
                        final_output = f"{run_result.stdout}\n{run_result.stderr}".strip()
                        final_error = "Self-heal attempts exhausted; still failing."
                        finding = _issue_finding(case_like, attempts)
                        break

                    diagnosis = llm.diagnose_and_fix_script(
                        name=requirement.name,
                        description=requirement.description,
                        test_case=case_like,
                        env_var_names=env_var_names,
                        readme=readme,
                        file_tree=file_tree,
                        script=script,
                        stdout=run_result.stdout,
                        stderr=run_result.stderr,
                        exit_code=run_result.exit_code,
                        read_file=read_file,
                        on_round=on_round,
                    )

                    if diagnosis.classification == "app_bug":
                        final_status = TestCaseExecutionStatus.FAILED
                        final_output = f"{run_result.stdout}\n{run_result.stderr}".strip()
                        final_error = diagnosis.explanation
                        finding = _bug_finding(diagnosis, case_like)
                        break

                    script = diagnosis.fixed_script

                case_exec.status = final_status
                case_exec.attempts = attempts
                case_exec.output = final_output[:_OUTPUT_MAX_CHARS] or None
                case_exec.error = final_error[:_OUTPUT_MAX_CHARS] if final_error else None
                case_exec.script_snapshot = script
                for field, value in finding.items():
                    setattr(case_exec, field, value)
                # Written even when there is no finding: on a passing case
                # this records where it passed, which is the same question a
                # reader asks of a failure.
                case_exec.environment = environment_utils.script_environment()
                case_exec.updated_at = _now()
                session.add(case_exec)

                if final_status in (TestCaseExecutionStatus.PASSED, TestCaseExecutionStatus.FAILED):
                    # Script is correct either way — worth reusing next
                    # time. Never cache on ERROR (still looks broken).
                    test_case.script = script
                    session.add(test_case)

                session.commit()

            execution.status = TestExecutionStatus.COMPLETED
            execution.last_heartbeat = None
            execution.retry_count = 0
            execution.updated_at = _now()
            session.add(execution)
            session.commit()
            logger.info("Test execution %d completed", test_execution_id)
        except Exception as exc:
            # Never re-raise: the DB retry counter, not RQ's failed registry,
            # is the recovery mechanism.
            logger.exception("Test execution failed for execution %d", test_execution_id)
            _record_failure(session, test_execution_id, exc)
