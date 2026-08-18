"""Tests for backend/tasks/explore_requirement.py.

``browser_session.BrowserSession`` and the ``services.llm`` functions are
monkeypatched wholesale, exactly as ``test_execute_test.py`` mocks
``script_runner`` — CI verifies the state machine, never a real browser.
"""

import json

import pytest

from backend.models.database import (
    SPRINT_FINISHED_ERROR,
    ExploratoryRun,
    ExploratoryRunStatus,
    ExploratorySessionStatus,
    RequirementStatus,
    TestEnvironmentAccess,
    TestEnvironmentStatus,
)
from backend.services import llm
from backend.services.browser_session import FindingRecord
from backend.tasks.explore_requirement import explore_requirement_task
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import _seed_exploratory_run, _seed_exploratory_session

ENV_VARS = {"APP_URL": "https://app.test", "ADMIN_PASSWORD": "hunter2"}


# ── fixtures / helpers ────────────────────────────────────────────────


def _seed_environment(db_session, sprint, env_vars=None):
    row = TestEnvironmentAccess(
        sprint_id=sprint.id,
        content="Access at https://app.test",
        original_content="Access at https://app.test",
        status=TestEnvironmentStatus.CONFIRMED,
        env_vars_json=json.dumps(env_vars if env_vars is not None else ENV_VARS),
    )
    db_session.add(row)
    db_session.commit()
    return row


def _seed_run_with_sessions(db_session, charters=("Explore export",), **run_kwargs):
    sprint = _seed_sprint(db_session)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    _seed_environment(db_session, sprint)
    run = _seed_exploratory_run(db_session, sprint, requirement, **run_kwargs)
    for position, charter in enumerate(charters):
        _seed_exploratory_session(db_session, run, position=position, charter=charter)
    db_session.refresh(run)
    return sprint, requirement, run


class _FakeBrowser:
    """Stands in for BrowserSession; records construction and yields tools."""

    instances: list["_FakeBrowser"] = []

    def __init__(self, base_urls, env_vars, on_finding, **kwargs):
        self.base_urls = base_urls
        self.env_vars = env_vars
        self.on_finding = on_finding
        self.closed = False
        _FakeBrowser.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def tool_registry(self):
        return {"snapshot": lambda **kw: "page"}


@pytest.fixture(autouse=True)
def _reset_fake_browser():
    _FakeBrowser.instances = []
    yield
    _FakeBrowser.instances = []


@pytest.fixture
def patched(monkeypatch):
    """Patch the browser and both LLM calls with controllable stubs."""
    from backend.tasks import explore_requirement as task_module

    monkeypatch.setattr(task_module.browser_session, "BrowserSession", _FakeBrowser)

    state = {
        "loop_calls": [],
        "loop_result": llm.ExplorationLoopResult(
            notes="Explored the export flow.",
            stop_reason=llm.STOP_CHARTER_COMPLETE,
            actions_used=7,
            action_log=["snapshot() -> page", "click(ref='e3') -> clicked"],
        ),
        "loop_error": None,
        "summary": "Export looks broadly sound.",
        "summary_error": None,
        "findings": [],
    }

    def fake_loop(**kwargs):
        state["loop_calls"].append(kwargs)
        browser = _FakeBrowser.instances[-1]
        for record in state["findings"]:
            browser.on_finding(record, b"PNGDATA")
        if state["loop_error"] is not None:
            raise state["loop_error"]
        return state["loop_result"]

    def fake_summary(**kwargs):
        state["summary_calls"] = state.get("summary_calls", [])
        state["summary_calls"].append(kwargs)
        if state["summary_error"] is not None:
            raise state["summary_error"]
        return llm.ExplorationSummaryResult(summary=state["summary"])

    monkeypatch.setattr(task_module.llm, "run_exploration_loop", fake_loop)
    monkeypatch.setattr(task_module.llm, "summarize_exploration", fake_summary)
    return state


# ── happy path ────────────────────────────────────────────────────────


class TestHappyPath:
    def test_completes_run_and_sessions(self, db_session, patched):
        _, _, run = _seed_run_with_sessions(db_session, charters=("A", "B"))

        explore_requirement_task(run.id)

        db_session.expire_all()
        refreshed = db_session.get(ExploratoryRun, run.id)
        assert refreshed.status == ExploratoryRunStatus.COMPLETED
        assert refreshed.error is None
        assert refreshed.last_heartbeat is None
        assert [s.status for s in refreshed.sessions] == [
            ExploratorySessionStatus.COMPLETED,
            ExploratorySessionStatus.COMPLETED,
        ]

    def test_persists_session_sheet(self, db_session, patched):
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        db_session.expire_all()
        session_row = db_session.get(ExploratoryRun, run.id).sessions[0]
        assert session_row.session_notes == "Explored the export flow."
        assert session_row.actions_used == 7
        assert session_row.stop_reason == llm.STOP_CHARTER_COMPLETE
        assert "snapshot()" in session_row.action_log

    def test_charters_run_in_position_order(self, db_session, patched):
        _, _, run = _seed_run_with_sessions(db_session, charters=("first", "second", "third"))

        explore_requirement_task(run.id)

        assert [call["charter"] for call in patched["loop_calls"]] == [
            "first",
            "second",
            "third",
        ]

    def test_one_browser_per_session_each_closed(self, db_session, patched):
        _, _, run = _seed_run_with_sessions(db_session, charters=("A", "B"))

        explore_requirement_task(run.id)

        assert len(_FakeBrowser.instances) == 2
        assert all(browser.closed for browser in _FakeBrowser.instances)

    def test_browser_receives_resolved_base_urls_and_env_vars(self, db_session, patched):
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        browser = _FakeBrowser.instances[0]
        assert browser.base_urls == ["https://app.test"]
        assert browser.env_vars == ENV_VARS

    def test_loop_receives_base_urls(self, db_session, patched):
        """The model only ever sees variable names, so the URLs must be passed."""
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        assert patched["loop_calls"][0]["base_urls"] == ["https://app.test"]

    def test_loop_receives_secrets_excluding_base_urls(self, db_session, patched):
        """Rewriting the base URLs would gut the action log while protecting nothing."""
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        # Name -> value, so the log can say *which* credential was typed.
        assert patched["loop_calls"][0]["secrets"] == {"ADMIN_PASSWORD": "hunter2"}

    def test_writes_summary(self, db_session, patched):
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        db_session.expire_all()
        assert db_session.get(ExploratoryRun, run.id).summary == "Export looks broadly sound."

    def test_summary_receives_every_session_sheet(self, db_session, patched):
        _, _, run = _seed_run_with_sessions(db_session, charters=("A", "B"))

        explore_requirement_task(run.id)

        sheets = patched["summary_calls"][-1]["sessions"]
        assert [sheet.charter for sheet in sheets] == ["A", "B"]
        assert all(sheet.session_notes == "Explored the export flow." for sheet in sheets)


# ── findings ──────────────────────────────────────────────────────────


class TestFindings:
    def _finding(self, title="Empty export", environment="Chromium 131 · viewport 1280x720"):
        return FindingRecord(
            finding_type="bug",
            severity="high",
            title=title,
            steps_to_reproduce="Open reports\nClick Export",
            expected="A CSV with a header",
            actual="Zero bytes",
            environment=environment,
        )

    def test_environment_is_persisted(self, db_session, patched, monkeypatch):
        from backend.tasks import explore_requirement as task_module

        monkeypatch.setattr(
            task_module.StorageService,
            "store_screenshot",
            lambda self, png, directory, session_id, position: None,
        )
        patched["findings"] = [self._finding(environment="Chromium 131 · https://app.test/x")]
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        db_session.expire_all()
        findings = db_session.get(ExploratoryRun, run.id).sessions[0].findings
        assert findings[0].environment == "Chromium 131 · https://app.test/x"

    def test_out_of_enum_severity_and_type_are_normalized(self, db_session, patched, monkeypatch):
        """The tool schema constrains both, but the model isn't bound by it.
        An unrecognised type would count toward finding_count while counting
        toward neither bug_count nor issue_count."""
        from backend.tasks import explore_requirement as task_module

        monkeypatch.setattr(
            task_module.StorageService,
            "store_screenshot",
            lambda self, png, directory, session_id, position: None,
        )
        record = self._finding()
        record.severity = "critical"
        record.finding_type = "defect"
        patched["findings"] = [record]
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        db_session.expire_all()
        findings = db_session.get(ExploratoryRun, run.id).sessions[0].findings
        assert findings[0].severity == "medium"
        assert findings[0].finding_type == "bug"

    def test_valid_issue_type_is_not_rewritten(self, db_session, patched, monkeypatch):
        """Normalizing must not collapse the SBTM distinction it exists to keep."""
        from backend.tasks import explore_requirement as task_module

        monkeypatch.setattr(
            task_module.StorageService,
            "store_screenshot",
            lambda self, png, directory, session_id, position: None,
        )
        record = self._finding()
        record.finding_type = "issue"
        patched["findings"] = [record]
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        db_session.expire_all()
        findings = db_session.get(ExploratoryRun, run.id).sessions[0].findings
        assert findings[0].finding_type == "issue"

    def test_finding_without_an_environment_still_persists(self, db_session, patched, monkeypatch):
        """The browser layer promises a string, but the column is nullable so
        rows predating capture read cleanly — neither path may lose a finding."""
        from backend.tasks import explore_requirement as task_module

        monkeypatch.setattr(
            task_module.StorageService,
            "store_screenshot",
            lambda self, png, directory, session_id, position: None,
        )
        patched["findings"] = [self._finding(environment=None)]
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        db_session.expire_all()
        findings = db_session.get(ExploratoryRun, run.id).sessions[0].findings
        assert len(findings) == 1
        assert findings[0].environment is None

    def test_findings_persist_with_screenshot(self, db_session, patched, monkeypatch):
        from backend.tasks import explore_requirement as task_module

        monkeypatch.setattr(
            task_module.StorageService,
            "store_screenshot",
            lambda self, png, directory, session_id, position: f"/tmp/s{session_id}_{position}.png",
        )
        patched["findings"] = [self._finding()]
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        db_session.expire_all()
        findings = db_session.get(ExploratoryRun, run.id).sessions[0].findings
        assert len(findings) == 1
        assert findings[0].title == "Empty export"
        assert findings[0].finding_type == "bug"
        expected = f"/tmp/s{findings[0].exploratory_session_id}_0.png"
        assert findings[0].screenshot_path == expected

    def test_findings_persist_without_screenshot_when_offline_disabled(
        self, db_session, patched, monkeypatch
    ):
        """STORE_OFFLINE=false is the normal no-screenshot case, not an error."""
        from backend.tasks import explore_requirement as task_module

        monkeypatch.setattr(
            task_module.StorageService,
            "store_screenshot",
            lambda self, png, directory, session_id, position: None,
        )
        patched["findings"] = [self._finding()]
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        db_session.expire_all()
        findings = db_session.get(ExploratoryRun, run.id).sessions[0].findings
        assert len(findings) == 1
        assert findings[0].screenshot_path is None

    def test_finding_positions_increment_within_a_session(self, db_session, patched, monkeypatch):
        from backend.tasks import explore_requirement as task_module

        monkeypatch.setattr(
            task_module.StorageService,
            "store_screenshot",
            lambda self, png, directory, session_id, position: None,
        )
        patched["findings"] = [self._finding("one"), self._finding("two")]
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        db_session.expire_all()
        findings = db_session.get(ExploratoryRun, run.id).sessions[0].findings
        assert [f.position for f in findings] == [0, 1]
        assert [f.title for f in findings] == ["one", "two"]

    def test_screenshot_write_failure_still_persists_the_finding(
        self, db_session, patched, monkeypatch
    ):
        from backend.tasks import explore_requirement as task_module

        def boom(self, png, directory, session_id, position):
            raise OSError("disk full")

        monkeypatch.setattr(task_module.StorageService, "store_screenshot", boom)
        patched["findings"] = [self._finding()]
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        db_session.expire_all()
        findings = db_session.get(ExploratoryRun, run.id).sessions[0].findings
        assert len(findings) == 1
        assert findings[0].screenshot_path is None


# ── resumability and failure ──────────────────────────────────────────


class TestResumability:
    def test_completed_sessions_are_skipped(self, db_session, patched):
        _, _, run = _seed_run_with_sessions(db_session, charters=("done", "todo"))
        run.sessions[0].status = ExploratorySessionStatus.COMPLETED
        db_session.add(run.sessions[0])
        db_session.commit()

        explore_requirement_task(run.id)

        assert [call["charter"] for call in patched["loop_calls"]] == ["todo"]

    def test_errored_sessions_are_skipped(self, db_session, patched):
        _, _, run = _seed_run_with_sessions(db_session, charters=("broken", "todo"))
        run.sessions[0].status = ExploratorySessionStatus.ERROR
        db_session.add(run.sessions[0])
        db_session.commit()

        explore_requirement_task(run.id)

        assert [call["charter"] for call in patched["loop_calls"]] == ["todo"]

    def test_running_session_restarts_from_scratch(self, db_session, patched):
        """A half-explored browser died with the worker — no partial resume."""
        _, _, run = _seed_run_with_sessions(db_session, charters=("interrupted",))
        run.sessions[0].status = ExploratorySessionStatus.RUNNING
        db_session.add(run.sessions[0])
        db_session.commit()

        explore_requirement_task(run.id)

        assert [call["charter"] for call in patched["loop_calls"]] == ["interrupted"]
        db_session.expire_all()
        assert (
            db_session.get(ExploratoryRun, run.id).sessions[0].status
            == ExploratorySessionStatus.COMPLETED
        )


class TestSessionFailure:
    def test_failing_session_marks_error_and_run_continues(self, db_session, patched, monkeypatch):
        """One bad charter must not abandon the rest of the run."""
        from backend.tasks import explore_requirement as task_module

        _, _, run = _seed_run_with_sessions(db_session, charters=("bad", "good"))

        calls = []

        def selective(**kwargs):
            calls.append(kwargs["charter"])
            if kwargs["charter"] == "bad":
                raise RuntimeError("browser exploded")
            return patched["loop_result"]

        monkeypatch.setattr(task_module.llm, "run_exploration_loop", selective)

        explore_requirement_task(run.id)

        assert calls == ["bad", "good"]
        db_session.expire_all()
        refreshed = db_session.get(ExploratoryRun, run.id)
        assert refreshed.sessions[0].status == ExploratorySessionStatus.ERROR
        assert "browser exploded" in refreshed.sessions[0].error
        assert refreshed.sessions[0].stop_reason == "error"
        assert refreshed.sessions[1].status == ExploratorySessionStatus.COMPLETED
        # The run itself still completes — partial results are real results.
        assert refreshed.status == ExploratoryRunStatus.COMPLETED


class TestSummaryFailure:
    def test_summary_error_leaves_null_summary_and_completes_run(self, db_session, patched):
        patched["summary_error"] = llm.LLMError("provider down")
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        db_session.expire_all()
        refreshed = db_session.get(ExploratoryRun, run.id)
        assert refreshed.summary is None
        assert refreshed.status == ExploratoryRunStatus.COMPLETED
        assert refreshed.error is None

    def test_summary_heartbeats_while_the_run_is_still_running(
        self, db_session, patched, monkeypatch
    ):
        """A retried summary must not look like a dead worker to the reconciler.

        The summary runs while the run is ``running``, so each of its attempts
        heartbeats — otherwise ``HEARTBEAT_STALE_SECONDS`` could elapse and the
        reconciler would re-enqueue a run that is nearly finished.
        """
        from backend.tasks import explore_requirement as task_module

        _, _, run = _seed_run_with_sessions(db_session)
        run_id = run.id
        observed: dict = {}

        def stub(**kwargs):
            kwargs["on_attempt"]()
            db_session.expire_all()
            row = db_session.get(ExploratoryRun, run_id)
            observed["status"] = row.status
            observed["heartbeat"] = row.last_heartbeat
            return llm.ExplorationSummaryResult(summary="ok")

        monkeypatch.setattr(task_module.llm, "summarize_exploration", stub)

        explore_requirement_task(run_id)

        assert observed["heartbeat"] is not None
        assert observed["status"] == ExploratoryRunStatus.RUNNING


# ── guards ────────────────────────────────────────────────────────────


class TestGuards:
    def test_missing_run_no_ops(self, db_session, patched):
        explore_requirement_task(999999)  # must not raise
        assert patched["loop_calls"] == []

    @pytest.mark.parametrize(
        "status", [ExploratoryRunStatus.COMPLETED, ExploratoryRunStatus.FAILED]
    )
    def test_stale_job_skipped(self, db_session, patched, status):
        _, _, run = _seed_run_with_sessions(db_session, status=status)

        explore_requirement_task(run.id)

        assert patched["loop_calls"] == []

    def test_inactive_sprint_fails_run(self, db_session, patched):
        sprint, requirement, run = _seed_run_with_sessions(db_session)
        sprint.active = False
        db_session.add(sprint)
        db_session.commit()

        explore_requirement_task(run.id)

        db_session.expire_all()
        refreshed = db_session.get(ExploratoryRun, run.id)
        assert refreshed.status == ExploratoryRunStatus.FAILED
        assert refreshed.error == SPRINT_FINISHED_ERROR
        assert patched["loop_calls"] == []

    def test_missing_env_vars_fails_run(self, db_session, patched):
        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        _seed_environment(db_session, sprint, env_vars={})
        run = _seed_exploratory_run(db_session, sprint, requirement)
        _seed_exploratory_session(db_session, run)

        explore_requirement_task(run.id)

        db_session.expire_all()
        refreshed = db_session.get(ExploratoryRun, run.id)
        assert refreshed.status == ExploratoryRunStatus.FAILED
        assert "environment access variables" in refreshed.error

    def test_unresolvable_base_url_fails_run(self, db_session, patched):
        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        _seed_environment(db_session, sprint, env_vars={"OTHER": "x"})
        run = _seed_exploratory_run(
            db_session, sprint, requirement, base_url_env_vars_csv="MISSING_URL"
        )
        _seed_exploratory_session(db_session, run)

        explore_requirement_task(run.id)

        db_session.expire_all()
        refreshed = db_session.get(ExploratoryRun, run.id)
        assert refreshed.status == ExploratoryRunStatus.FAILED
        assert "No application URL" in refreshed.error


class TestUnreachedSessionsAreSettled:
    """A terminal run must leave no charter session reading as in-flight.

    The mirror of ``test_execute_test.TestUnreachedCasesAreSettled`` — the
    session rows have exactly one writer (``_run_one_session``), so every
    early exit used to strand the charters it never reached.
    """

    def test_job_start_guard_settles_its_sessions(self, db_session, patched):
        sprint, _, run = _seed_run_with_sessions(db_session, charters=("first", "second"))
        sprint.active = False
        db_session.add(sprint)
        db_session.commit()

        explore_requirement_task(run.id)

        db_session.expire_all()
        refreshed = db_session.get(ExploratoryRun, run.id)
        assert refreshed.status == ExploratoryRunStatus.FAILED
        assert [s.status for s in refreshed.sessions] == [ExploratorySessionStatus.SKIPPED] * 2
        assert all(s.error.startswith("Not run.") for s in refreshed.sessions)
        assert patched["loop_calls"] == []

    def test_superseded_mid_run_settles_the_remaining_charters(
        self, db_session, patched, monkeypatch
    ):
        """The scenario this whole mechanism exists for, on the browser path.

        An upstream edit lands while charter one is exploring; the run stops
        at the boundary, and charter two must not sit "Queued" forever under
        a failed run that can no longer be restarted.
        """
        from backend.database import new_session
        from backend.models.database import SUPERSEDED_ERROR, Requirement
        from backend.tasks import explore_requirement as task_module

        sprint, requirement, run = _seed_run_with_sessions(db_session, charters=("first", "second"))
        run.requirement_revision = requirement.content_revision
        run.plan_revision = 0
        run.env_revision = sprint.test_environment.content_revision
        db_session.add(run)
        db_session.commit()
        requirement_id, run_id = requirement.id, run.id

        def bump_then_explore(**kwargs):
            # Commit the cascade from another session, as the route would.
            with new_session() as other:
                row = other.get(Requirement, requirement_id)
                row.content_revision += 1
                other.add(row)
                other.commit()
            return patched["loop_result"]

        monkeypatch.setattr(task_module.llm, "run_exploration_loop", bump_then_explore)

        explore_requirement_task(run_id)

        db_session.expire_all()
        refreshed = db_session.get(ExploratoryRun, run_id)
        assert refreshed.status == ExploratoryRunStatus.FAILED
        assert refreshed.error == SUPERSEDED_ERROR
        assert refreshed.sessions[0].status == ExploratorySessionStatus.COMPLETED
        assert refreshed.sessions[1].status == ExploratorySessionStatus.SKIPPED
        assert SUPERSEDED_ERROR in refreshed.sessions[1].error

    def test_findings_survive_a_settled_run(self, db_session, patched):
        """Settling touches statuses only — recorded observations stay real."""
        from backend.tests.test_sprints import _seed_exploratory_finding

        sprint, _, run = _seed_run_with_sessions(db_session, charters=("first", "second"))
        finding = _seed_exploratory_finding(db_session, run.sessions[0])
        sprint.active = False
        db_session.add(sprint)
        db_session.commit()

        explore_requirement_task(run.id)

        db_session.expire_all()
        refreshed = db_session.get(ExploratoryRun, run.id)
        assert [f.id for f in refreshed.sessions[0].findings] == [finding.id]


class TestRetryDisposition:
    def test_repends_below_the_retry_cap(self, db_session, patched, monkeypatch):
        from backend.services import finalization
        from backend.tasks import explore_requirement as task_module

        # The retry protocol lives in `finalization.record_failure` now, so
        # the cap is read there rather than in the task module.
        monkeypatch.setattr(finalization, "MAX_AUTO_RETRIES", 3)
        _, _, run = _seed_run_with_sessions(db_session)

        # Blow up outside the per-session guard so _record_failure runs.
        monkeypatch.setattr(
            task_module, "_write_summary", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        )

        explore_requirement_task(run.id)

        db_session.expire_all()
        refreshed = db_session.get(ExploratoryRun, run.id)
        assert refreshed.status == ExploratoryRunStatus.PENDING
        assert refreshed.retry_count == 1
        assert refreshed.last_heartbeat is None

    def test_fails_at_the_retry_cap(self, db_session, patched, monkeypatch):
        from backend.services import finalization
        from backend.tasks import explore_requirement as task_module

        monkeypatch.setattr(finalization, "MAX_AUTO_RETRIES", 1)
        _, _, run = _seed_run_with_sessions(db_session)
        monkeypatch.setattr(
            task_module,
            "_write_summary",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaput")),
        )

        explore_requirement_task(run.id)

        db_session.expire_all()
        refreshed = db_session.get(ExploratoryRun, run.id)
        assert refreshed.status == ExploratoryRunStatus.FAILED
        assert "kaput" in refreshed.error
        # Terminal, so nothing may stay in flight. The one charter here had
        # already completed, so it keeps its result — only unreached rows move.
        assert refreshed.sessions[0].status == ExploratorySessionStatus.COMPLETED


class TestHeartbeat:
    def test_on_round_commits_a_fresh_heartbeat(self, db_session, patched, monkeypatch):
        """A live session must not be swept as a crashed worker."""
        from backend.tasks import explore_requirement as task_module

        _, _, run = _seed_run_with_sessions(db_session)
        observed: list = []

        def capture(**kwargs):
            kwargs["on_round"](3)
            db_session.expire_all()
            observed.append(db_session.get(ExploratoryRun, run.id).last_heartbeat)
            return patched["loop_result"]

        monkeypatch.setattr(task_module.llm, "run_exploration_loop", capture)

        explore_requirement_task(run.id)

        assert observed and observed[0] is not None

    def test_heartbeat_cleared_when_run_completes(self, db_session, patched):
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)

        db_session.expire_all()
        assert db_session.get(ExploratoryRun, run.id).last_heartbeat is None


class TestLiveActionCount:
    """``actions_used`` must climb during the session, not appear at the end."""

    def test_on_round_publishes_the_count_mid_session(self, db_session, patched, monkeypatch):
        from backend.tasks import explore_requirement as task_module

        _, _, run = _seed_run_with_sessions(db_session)
        observed: list[int] = []

        def capture(**kwargs):
            for count in (1, 2, 5):
                kwargs["on_round"](count)
                db_session.expire_all()
                observed.append(db_session.get(ExploratoryRun, run.id).sessions[0].actions_used)
            return patched["loop_result"]

        monkeypatch.setattr(task_module.llm, "run_exploration_loop", capture)

        explore_requirement_task(run.id)

        assert observed == [1, 2, 5]
        db_session.expire_all()
        # The loop's own result still has the last word.
        assert db_session.get(ExploratoryRun, run.id).sessions[0].actions_used == 7

    def test_each_session_publishes_its_own_count(self, db_session, patched, monkeypatch):
        from backend.tasks import explore_requirement as task_module

        _, _, run = _seed_run_with_sessions(db_session, charters=("A", "B"))
        observed: list[list[int]] = []

        def capture(**kwargs):
            kwargs["on_round"](4)
            db_session.expire_all()
            observed.append(
                [s.actions_used for s in db_session.get(ExploratoryRun, run.id).sessions]
            )
            return patched["loop_result"]

        monkeypatch.setattr(task_module.llm, "run_exploration_loop", capture)

        explore_requirement_task(run.id)

        # The second charter's rounds must not overwrite the first sheet.
        assert observed == [[4, 0], [7, 4]]

    def test_partial_count_survives_a_failed_session(self, db_session, patched, monkeypatch):
        from backend.tasks import explore_requirement as task_module

        _, _, run = _seed_run_with_sessions(db_session)

        def capture(**kwargs):
            kwargs["on_round"](6)
            raise RuntimeError("browser exploded")

        monkeypatch.setattr(task_module.llm, "run_exploration_loop", capture)

        explore_requirement_task(run.id)

        db_session.expire_all()
        session_row = db_session.get(ExploratoryRun, run.id).sessions[0]
        assert session_row.status == ExploratorySessionStatus.ERROR
        assert session_row.actions_used == 6

    def test_restarted_charter_resets_a_stale_count(self, db_session, patched, monkeypatch):
        """A retried charter explores from scratch, so its old count is meaningless."""
        from backend.tasks import explore_requirement as task_module

        _, _, run = _seed_run_with_sessions(db_session)
        session_row = run.sessions[0]
        session_row.status = ExploratorySessionStatus.RUNNING
        session_row.actions_used = 19
        db_session.add(session_row)
        db_session.commit()

        observed: list[int] = []

        def capture(**kwargs):
            db_session.expire_all()
            observed.append(db_session.get(ExploratoryRun, run.id).sessions[0].actions_used)
            return patched["loop_result"]

        monkeypatch.setattr(task_module.llm, "run_exploration_loop", capture)

        explore_requirement_task(run.id)

        assert observed == [0]


class TestFindingExportWiring:
    """Same rule as the scripted task: only a run that finished reports."""

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

    def test_called_once_when_the_run_completes(self, db_session, patched, export_spy):
        _, _, run = _seed_run_with_sessions(db_session, charters=("A", "B"))

        explore_requirement_task(run.id)

        assert export_spy == [run.id]

    def test_not_called_when_the_sprint_was_finished(self, db_session, patched, export_spy):
        sprint, _, run = _seed_run_with_sessions(db_session)
        sprint.active = False
        db_session.add(sprint)
        db_session.commit()

        explore_requirement_task(run.id)

        assert export_spy == []

    def test_not_called_when_the_run_is_superseded_mid_run(self, db_session, patched, export_spy):
        """An upstream edit leaves a finding set that is incomplete and
        known to be — the Retry button is where a human decides."""
        sprint, requirement, run = _seed_run_with_sessions(db_session, charters=("A", "B"))
        requirement.content_revision += 1
        db_session.add(requirement)
        db_session.commit()

        explore_requirement_task(run.id)

        db_session.expire_all()
        assert db_session.get(ExploratoryRun, run.id).status == ExploratoryRunStatus.FAILED
        assert export_spy == []

    def test_not_called_on_a_stale_job(self, db_session, patched, export_spy):
        _, _, run = _seed_run_with_sessions(db_session, status=ExploratoryRunStatus.COMPLETED)

        explore_requirement_task(run.id)

        assert export_spy == []

    def test_a_raising_exporter_cannot_fail_the_run(self, db_session, patched, monkeypatch):
        import backend.services.finding_export as module

        def _boom(session, parent):
            raise RuntimeError("tracker exploded")

        monkeypatch.setattr(module, "export_findings", _boom)
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)  # must not raise

        db_session.expire_all()
        assert db_session.get(ExploratoryRun, run.id).status == ExploratoryRunStatus.COMPLETED


class TestDefectGroupingWiring:
    """Same placement as the scripted task, and the same ordering rule:
    grouping commits before export files anything."""

    @pytest.fixture
    def order(self, monkeypatch):
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

    def test_grouping_precedes_export_on_the_completion_path(self, db_session, patched, order):
        _, _, run = _seed_run_with_sessions(db_session, charters=("A", "B"))

        explore_requirement_task(run.id)

        assert order == [("group", run.id), ("export", run.id)]

    def test_not_called_when_the_run_is_superseded_mid_run(self, db_session, patched, order):
        sprint, requirement, run = _seed_run_with_sessions(db_session, charters=("A", "B"))
        requirement.content_revision += 1
        db_session.add(requirement)
        db_session.commit()

        explore_requirement_task(run.id)

        db_session.expire_all()
        assert db_session.get(ExploratoryRun, run.id).status == ExploratoryRunStatus.FAILED
        assert order == []

    def test_a_raising_grouping_pass_cannot_fail_the_run(self, db_session, patched, monkeypatch):
        import backend.services.finding_grouping as module

        def _boom(session, parent):
            raise RuntimeError("grouping exploded")

        monkeypatch.setattr(module, "assign_defect_groups", _boom)
        _, _, run = _seed_run_with_sessions(db_session)

        explore_requirement_task(run.id)  # must not raise

        db_session.expire_all()
        assert db_session.get(ExploratoryRun, run.id).status == ExploratoryRunStatus.COMPLETED
