"""Tests for backend/services/finding_export.py.

``services.issue_tracker`` is monkeypatched throughout — the same
treatment ``services.script_runner.run_script`` gets in
``test_execute_test.py``.  Grouping runs for real against its
deterministic prefilter, with ``llm.group_findings`` stubbed.
"""

import json

import pytest

from backend.models.database import (
    ExploratoryRunStatus,
    IssueTrackerConfig,
    RequirementStatus,
    TestCaseExecutionStatus,
    TestEnvironmentStatus,
)
from backend.services import finding_export, issue_tracker, llm
from backend.services.issue_tracker import IssueRef, TrackerError, TrackerUnavailableError
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import (
    _seed_exploratory_finding,
    _seed_exploratory_run,
    _seed_exploratory_session,
    _seed_test_case,
    _seed_test_case_execution,
    _seed_test_execution,
    _seed_test_plan,
    _seed_test_run,
)
from backend.utils.crypto import encrypt_token

ENV_VARS = {"APP_URL": "https://app.test", "ADMIN_PASSWORD": "s3cr3t-passw0rd"}


# ── Stubs ─────────────────────────────────────────────────────────────


class _TrackerStub:
    """Records every call and answers however the test configured it."""

    def __init__(self):
        self.created: list = []
        self.open_checks: list[str] = []
        self.attachments: list = []
        self.create_error: Exception | None = None
        self.is_open = True
        self._next = 1

    def create_issue(self, config, report, context):
        self.created.append((config, report, context))
        if self.create_error is not None:
            raise self.create_error
        key = f"QA-{self._next}"
        self._next += 1
        return IssueRef(key=key, url=f"https://acme.atlassian.net/browse/{key}")

    def issue_is_open(self, config, key):
        self.open_checks.append(key)
        return self.is_open

    def attach_screenshot(self, config, key, png, filename):
        self.attachments.append((key, filename, png))


@pytest.fixture
def tracker(monkeypatch):
    stub = _TrackerStub()
    monkeypatch.setattr(issue_tracker, "create_issue", stub.create_issue)
    monkeypatch.setattr(issue_tracker, "issue_is_open", stub.issue_is_open)
    monkeypatch.setattr(issue_tracker, "attach_screenshot", stub.attach_screenshot)
    # Grouping's LLM stage is never allowed near the network here; the
    # deterministic prefilter is what these tests exercise.
    monkeypatch.setattr(llm, "group_findings", _no_llm_grouping)
    return stub


def _no_llm_grouping(candidates, already_filed):
    raise llm.LLMError("stubbed out — prefilter only")


# ── Seeding ───────────────────────────────────────────────────────────


def _seed_environment(db_session, sprint, env_vars=None):
    from backend.models.database import TestEnvironmentAccess

    row = TestEnvironmentAccess(
        sprint_id=sprint.id,
        content="Access at https://app.test",
        original_content="Access at https://app.test",
        status=TestEnvironmentStatus.CONFIRMED,
        env_vars_json=json.dumps(ENV_VARS if env_vars is None else env_vars),
    )
    db_session.add(row)
    db_session.commit()
    return row


def _seed_tracker(db_session, sprint, provider="jira", target="QA"):
    config = IssueTrackerConfig(
        sprint_id=sprint.id,
        provider=provider,
        target=target,
        api_token=encrypt_token("dummy-token"),
        base_url="https://acme.atlassian.net" if provider == "jira" else None,
        account_email="qa@acme.test" if provider == "jira" else None,
        issue_type="Bug" if provider == "jira" else None,
    )
    db_session.add(config)
    db_session.commit()
    db_session.refresh(config)
    return config


def _finding_fields(title="Checkout returns 500", severity="high", **overrides):
    fields = {
        "finding_severity": severity,
        "finding_title": title,
        "finding_steps_to_reproduce": "Open /checkout\nSubmit",
        "finding_expected": "The order is created",
        "finding_actual": "HTTP 500",
        "environment": "Windows-10 · Python 3.12.4",
    }
    fields.update(overrides)
    return fields


def _scripted_run(
    db_session,
    *,
    export_findings=True,
    case_count=1,
    status=TestCaseExecutionStatus.FAILED,
    titles=None,
    with_tracker=True,
):
    """A completed scripted execution holding *case_count* bug findings."""
    sprint = _seed_sprint(db_session)
    _seed_environment(db_session, sprint)
    if with_tracker:
        _seed_tracker(db_session, sprint)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    plan = _seed_test_plan(db_session, requirement)
    run = _seed_test_run(db_session, sprint, export_findings=export_findings)
    execution = _seed_test_execution(db_session, run, requirement)
    for index in range(case_count):
        case = _seed_test_case(db_session, plan, position=index)
        title = titles[index] if titles else "Checkout returns 500"
        _seed_test_case_execution(
            db_session,
            execution,
            case,
            status=status,
            **(_finding_fields(title=title) if status != TestCaseExecutionStatus.PASSED else {}),
        )
    db_session.refresh(execution)
    return sprint, execution


def _exploratory_run(db_session, *, export_findings=True, finding_count=1, **finding_kwargs):
    sprint = _seed_sprint(db_session)
    _seed_environment(db_session, sprint)
    _seed_tracker(db_session, sprint)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    run = _seed_exploratory_run(
        db_session,
        sprint,
        requirement,
        status=ExploratoryRunStatus.COMPLETED,
        export_findings=export_findings,
    )
    exploratory_session = _seed_exploratory_session(db_session, run)
    for index in range(finding_count):
        _seed_exploratory_finding(db_session, exploratory_session, position=index, **finding_kwargs)
    db_session.refresh(run)
    return sprint, run


# ── Fast exits ────────────────────────────────────────────────────────


def test_flag_off_makes_no_tracker_call(db_session, tracker):
    """The fast exit is what makes calling export from every completion
    path free — and why every pre-existing run test is unaffected."""
    _, execution = _scripted_run(db_session, export_findings=False)

    outcome = finding_export.export_findings(db_session, execution)

    assert outcome == finding_export.ExportOutcome()
    assert tracker.created == []


def test_an_explicit_request_files_a_run_whose_toggle_was_off(db_session, tracker):
    """The toggle decides what happens *unasked*.

    A run started before a tracker was connected has its bugs unfiled by
    design, and the run page's button is the only way they ever get
    filed — so reading the toggle there would leave the user pressing
    something that silently does nothing.
    """
    _, execution = _scripted_run(db_session, export_findings=False)

    outcome = finding_export.export_findings(db_session, execution, requested=True)

    assert outcome.filed == 1
    db_session.expire_all()
    assert execution.cases[0].tracker_issue_key == "QA-1"


def test_an_explicit_request_files_an_exploratory_run_whose_toggle_was_off(db_session, tracker):
    _, run = _exploratory_run(db_session, export_findings=False)

    outcome = finding_export.export_findings(db_session, run, requested=True)

    assert outcome.filed == 1
    db_session.expire_all()
    assert run.sessions[0].findings[0].tracker_issue_key == "QA-1"


def test_a_request_still_files_nothing_when_there_is_nothing_to_file(db_session, tracker):
    """The `not rows` half of the fast exit stays unconditional, so the
    button is a no-op rather than a trap on a run with no bugs."""
    _, execution = _scripted_run(db_session, status=TestCaseExecutionStatus.PASSED)

    outcome = finding_export.export_findings(db_session, execution, requested=True)

    assert outcome == finding_export.ExportOutcome()
    assert tracker.created == []


def test_a_passing_run_files_nothing(db_session, tracker):
    _, execution = _scripted_run(db_session, status=TestCaseExecutionStatus.PASSED)

    finding_export.export_findings(db_session, execution)

    assert tracker.created == []


def test_issue_findings_are_never_exported(db_session, tracker):
    """An `error` case says the testing was obstructed, not that the
    product is wrong — there is no bug to report."""
    _, execution = _scripted_run(db_session, status=TestCaseExecutionStatus.ERROR)

    finding_export.export_findings(db_session, execution)

    assert tracker.created == []


def test_unknown_parent_type_is_a_no_op(db_session, tracker):
    assert finding_export.export_findings(db_session, object()) == finding_export.ExportOutcome()


# ── Filing ────────────────────────────────────────────────────────────


def test_files_one_issue_and_records_the_receipt(db_session, tracker):
    _, execution = _scripted_run(db_session)

    outcome = finding_export.export_findings(db_session, execution)

    assert outcome.filed == 1
    db_session.expire_all()
    case = execution.cases[0]
    assert case.tracker_issue_key == "QA-1"
    assert case.tracker_issue_url == "https://acme.atlassian.net/browse/QA-1"
    assert case.tracker_target == "jira:QA"
    assert case.tracker_is_duplicate is False
    assert case.tracker_error is None


def test_eight_identical_failures_become_one_ticket(db_session, tracker):
    """The whole point of grouping: one broken dependency fails every case
    in a plan, and that is one defect."""
    _, execution = _scripted_run(db_session, case_count=8)

    outcome = finding_export.export_findings(db_session, execution)

    assert len(tracker.created) == 1
    assert outcome.filed == 1
    assert outcome.linked == 7
    db_session.expire_all()
    keys = {case.tracker_issue_key for case in execution.cases}
    assert keys == {"QA-1"}
    duplicates = [case for case in execution.cases if case.tracker_is_duplicate]
    assert len(duplicates) == 7


def test_grouped_members_are_listed_under_also_observed(db_session, tracker):
    """Nothing is appended to the ticket afterwards, so this list is its
    entire record of how often the defect was seen."""
    _, execution = _scripted_run(db_session, case_count=3)

    finding_export.export_findings(db_session, execution)

    context = tracker.created[0][2]
    assert len(context.also_observed) == 2
    assert all("Scripted run" in entry for entry in context.also_observed)


def test_distinct_findings_each_get_a_ticket(db_session, tracker):
    _, execution = _scripted_run(
        db_session, case_count=2, titles=["Checkout returns 500", "Tax is omitted"]
    )

    outcome = finding_export.export_findings(db_session, execution)

    assert outcome.filed == 2
    db_session.expire_all()
    assert {case.tracker_issue_key for case in execution.cases} == {"QA-1", "QA-2"}


def test_every_write_sets_tracker_target(db_session, tracker):
    """Without it the de-duplication window cannot be scoped, which is
    what keeps an edited config from adopting a foreign issue key."""
    _, execution = _scripted_run(db_session, case_count=4)

    finding_export.export_findings(db_session, execution)

    db_session.expire_all()
    assert {case.tracker_target for case in execution.cases} == {"jira:QA"}


def test_context_carries_the_provenance(db_session, tracker):
    sprint, execution = _scripted_run(db_session)

    finding_export.export_findings(db_session, execution)

    context = tracker.created[0][2]
    assert context.sprint_name == sprint.name
    assert context.source_kind == "scripted"
    assert context.run_label.startswith("Scripted run ")


def test_secret_values_reach_the_transport_without_the_base_urls(db_session, tracker):
    """A bug report has to be allowed to name the page it is about."""
    _, execution = _scripted_run(db_session)

    finding_export.export_findings(db_session, execution)

    secrets = tracker.created[0][2].secret_values
    assert "s3cr3t-passw0rd" in secrets
    assert "https://app.test" not in secrets


# ── Idempotency ───────────────────────────────────────────────────────


def test_second_call_after_success_is_a_no_op(db_session, tracker):
    _, execution = _scripted_run(db_session, case_count=3)
    finding_export.export_findings(db_session, execution)

    outcome = finding_export.export_findings(db_session, execution)

    assert len(tracker.created) == 1
    assert outcome == finding_export.ExportOutcome()


# ── Adopting an existing ticket ───────────────────────────────────────


def _file_first_run(db_session, tracker, sprint, title="Checkout returns 500"):
    """File one finding through a prior run, so a ticket exists."""
    requirement = _seed_requirement(
        db_session, sprint, name="Earlier", status=RequirementStatus.CONFIRMED
    )
    plan = _seed_test_plan(db_session, requirement)
    run = _seed_test_run(db_session, sprint, export_findings=True)
    execution = _seed_test_execution(db_session, run, requirement)
    _seed_test_case_execution(
        db_session,
        execution,
        _seed_test_case(db_session, plan),
        status=TestCaseExecutionStatus.FAILED,
        **_finding_fields(title=title),
    )
    db_session.refresh(execution)
    finding_export.export_findings(db_session, execution)
    return execution


def test_an_open_matching_ticket_is_adopted_not_re_filed(db_session, tracker):
    sprint, execution = _scripted_run(db_session)
    _file_first_run(db_session, tracker, sprint)
    tracker.created.clear()

    outcome = finding_export.export_findings(db_session, execution)

    assert tracker.created == []  # adopted — and never commented on
    assert tracker.open_checks == ["QA-1"]
    assert outcome.linked == 1
    db_session.expire_all()
    assert execution.cases[0].tracker_issue_key == "QA-1"


def test_the_adopted_url_is_read_back_not_reconstructed(db_session, tracker):
    """The URL shape differs per provider and per Jira site — this module
    has no business knowing either."""
    sprint, execution = _scripted_run(db_session)
    _file_first_run(db_session, tracker, sprint)

    finding_export.export_findings(db_session, execution)

    db_session.expire_all()
    assert execution.cases[0].tracker_issue_url == "https://acme.atlassian.net/browse/QA-1"


def test_the_adopted_url_is_read_back_from_this_sprint_only(db_session, tracker):
    """Two sprints can share one tracker, and Jira keys are per-project —
    so the same key exists in both. The read-back is scoped through the
    run to the sprint, exactly as the de-duplication window is."""
    # Seeded first, so an unscoped scan would find this row's URL first.
    other_sprint, other_execution = _scripted_run(db_session)
    other_case = other_execution.cases[0]
    other_case.tracker_issue_key = "QA-1"
    other_case.tracker_issue_url = "https://wrong-sprint.example/browse/QA-1"
    other_case.tracker_target = "jira:QA"
    db_session.add(other_case)
    db_session.commit()

    sprint, execution = _scripted_run(db_session)
    _file_first_run(db_session, tracker, sprint)

    finding_export.export_findings(db_session, execution)

    db_session.expire_all()
    assert execution.cases[0].tracker_issue_url == "https://acme.atlassian.net/browse/QA-1"


def test_a_closed_ticket_yields_a_new_one_with_a_back_reference(db_session, tracker):
    """Closing is a decision somebody made; reopening it silently would
    undo that."""
    sprint, execution = _scripted_run(db_session)
    _file_first_run(db_session, tracker, sprint)
    tracker.created.clear()
    tracker.is_open = False

    outcome = finding_export.export_findings(db_session, execution)

    assert outcome.filed == 1
    assert tracker.created[0][2].superseded_key == "QA-1"
    db_session.expire_all()
    assert execution.cases[0].tracker_issue_key == "QA-2"


def test_a_failed_state_check_files_fresh(db_session, tracker, monkeypatch):
    """`issue_is_open` never raises and answers False on any doubt, so a
    tracker outage costs a duplicate rather than a lost finding."""
    sprint, execution = _scripted_run(db_session)
    _file_first_run(db_session, tracker, sprint)
    tracker.created.clear()
    monkeypatch.setattr(issue_tracker, "issue_is_open", lambda config, key: False)

    outcome = finding_export.export_findings(db_session, execution)

    assert outcome.filed == 1


def test_findings_filed_under_another_target_are_excluded(db_session, tracker):
    """Asserted for GitHub specifically: its issue numbers are per-repo
    integers, so repo B's #7 would answer issue_is_open for repo A's #7
    and the finding would be attached to an unrelated ticket."""
    sprint, execution = _scripted_run(db_session, with_tracker=False)
    _seed_tracker(db_session, sprint, provider="github", target="acme/old")
    _file_first_run(db_session, tracker, sprint)

    # Re-point the sprint at a different repo, exactly as an edit would.
    db_session.expire_all()
    sprint.issue_tracker.target = "acme/new"
    db_session.commit()
    tracker.created.clear()
    tracker.open_checks.clear()

    outcome = finding_export.export_findings(db_session, execution)

    assert tracker.open_checks == []  # the old key was never even considered
    assert outcome.filed == 1
    db_session.expire_all()
    assert execution.cases[0].tracker_target == "github:acme/new"


# ── Failure handling ──────────────────────────────────────────────────


def test_a_create_failure_marks_every_member_and_files_nothing(db_session, tracker):
    """A retry has to re-elect and re-file the whole group, which it can
    only do if none of them carries a key."""
    _, execution = _scripted_run(db_session, case_count=3)
    tracker.create_error = TrackerError("Jira rejected the request (403)")

    outcome = finding_export.export_findings(db_session, execution)

    assert outcome.failed == 3
    assert outcome.filed == 0
    db_session.expire_all()
    for case in execution.cases:
        assert case.tracker_issue_key is None
        assert "403" in case.tracker_error


def test_a_second_call_after_a_failure_succeeds(db_session, tracker):
    _, execution = _scripted_run(db_session, case_count=3)
    tracker.create_error = TrackerUnavailableError("down")
    finding_export.export_findings(db_session, execution)
    tracker.create_error = None

    outcome = finding_export.export_findings(db_session, execution)

    assert outcome.filed == 1
    assert outcome.linked == 2
    db_session.expire_all()
    assert all(case.tracker_error is None for case in execution.cases)


def test_a_disconnected_tracker_is_recorded_on_the_findings(db_session, tracker):
    """Silently doing nothing would leave the run page with no way to
    explain why nothing was filed."""
    _, execution = _scripted_run(db_session, with_tracker=False)

    outcome = finding_export.export_findings(db_session, execution)

    assert outcome.failed == 1
    db_session.expire_all()
    assert "No issue tracker" in execution.cases[0].tracker_error


def test_an_unreadable_token_is_recorded_rather_than_raised(db_session, tracker):
    sprint, execution = _scripted_run(db_session, with_tracker=False)
    config = _seed_tracker(db_session, sprint)
    config.api_token = "not-fernet"
    db_session.commit()

    outcome = finding_export.export_findings(db_session, execution)

    assert outcome.failed == 1
    assert tracker.created == []


def test_an_unexpected_exception_never_escapes(db_session, tracker, monkeypatch):
    """It runs after a run is already `completed`, so raising would turn a
    finished run into a retry that re-executes every case."""
    _, execution = _scripted_run(db_session)
    monkeypatch.setattr(
        finding_export, "_spec_for", lambda parent: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    assert finding_export.export_findings(db_session, execution) == finding_export.ExportOutcome()


# ── Exploratory findings ──────────────────────────────────────────────


def test_exploratory_findings_export_with_their_own_label(db_session, tracker):
    _, run = _exploratory_run(db_session)

    outcome = finding_export.export_findings(db_session, run)

    assert outcome.filed == 1
    context = tracker.created[0][2]
    assert context.source_kind == "exploratory"
    assert context.run_label.startswith("Exploratory run ")
    db_session.expire_all()
    assert run.sessions[0].findings[0].tracker_issue_key == "QA-1"


def test_exploratory_issue_findings_are_skipped(db_session, tracker):
    _, run = _exploratory_run(db_session, finding_type="issue")

    finding_export.export_findings(db_session, run)

    assert tracker.created == []


def test_the_exploratory_toggle_is_honoured(db_session, tracker):
    _, run = _exploratory_run(db_session, export_findings=False)

    finding_export.export_findings(db_session, run)

    assert tracker.created == []


def test_a_screenshot_is_attached_to_the_representative(db_session, tracker, tmp_path):
    image = tmp_path / "finding-0.png"
    image.write_bytes(b"\x89PNG-data")
    _, run = _exploratory_run(db_session, screenshot_path=str(image))

    finding_export.export_findings(db_session, run)

    assert len(tracker.attachments) == 1
    key, filename, png = tracker.attachments[0]
    assert (key, filename, png) == ("QA-1", "finding-0.png", b"\x89PNG-data")


def test_a_missing_screenshot_is_not_an_error(db_session, tracker):
    """STORE_OFFLINE=false makes the absence normal, not a failure."""
    _, run = _exploratory_run(db_session, screenshot_path=None)

    outcome = finding_export.export_findings(db_session, run)

    assert outcome.filed == 1
    assert tracker.attachments == []


def test_a_screenshot_path_that_no_longer_exists_is_skipped(db_session, tracker, tmp_path):
    _, run = _exploratory_run(db_session, screenshot_path=str(tmp_path / "gone.png"))

    outcome = finding_export.export_findings(db_session, run)

    assert outcome.filed == 1
    assert tracker.attachments == []


def test_duplicates_contribute_no_attachment(db_session, tracker, tmp_path):
    """A duplicate files nothing, so it has no ticket to illustrate."""
    image = tmp_path / "finding-0.png"
    image.write_bytes(b"\x89PNG")
    _, run = _exploratory_run(db_session, finding_count=3, screenshot_path=str(image))

    finding_export.export_findings(db_session, run)

    assert len(tracker.attachments) == 1
