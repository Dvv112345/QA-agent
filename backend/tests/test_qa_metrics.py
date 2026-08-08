"""Unit tests for the QA metrics aggregator.

Built from real model instances but no database: the models are ordinary
Python objects until something persists them, and relationship attributes
work on transient instances.  That is deliberate rather than convenient —
it gives the tests the *derived* properties (``TestRun.status``,
``TestCaseExecution.finding_type``) for free, so the counting rules are
pinned against the same definitions production reads instead of against a
fake's restatement of them.
"""

from backend.models.database import (
    ExploratoryFinding,
    ExploratoryRun,
    ExploratoryRunStatus,
    ExploratorySession,
    ExploratorySessionStatus,
    Requirement,
    RequirementStatus,
    Sprint,
    TestCaseExecution,
    TestCaseExecutionStatus,
    TestExecution,
    TestExecutionStatus,
    TestRun,
)
from backend.services.qa_metrics import compute_sprint_metrics

# ── builders ──────────────────────────────────────────────────────────


def _case(
    test_case_id: int,
    status: str,
    *,
    title: str | None = None,
    severity: str = "medium",
    expected: str = "",
    actual: str = "",
    key: str | None = None,
    target: str | None = None,
) -> TestCaseExecution:
    return TestCaseExecution(
        test_case_id=test_case_id,
        status=status,
        finding_severity=severity if title else None,
        finding_title=title,
        finding_steps_to_reproduce="do the thing" if title else None,
        finding_expected=expected,
        finding_actual=actual,
        tracker_issue_key=key,
        tracker_target=target,
    )


def _execution(
    requirement_id: int,
    cases: list[TestCaseExecution],
    status: str = TestExecutionStatus.COMPLETED,
) -> TestExecution:
    execution = TestExecution(requirement_id=requirement_id, status=status)
    execution.cases = cases
    return execution


def _run(executions: list[TestExecution]) -> TestRun:
    run = TestRun(sprint_id=1)
    run.executions = executions
    return run


def _finding(
    title: str,
    *,
    finding_type: str = "bug",
    severity: str = "medium",
    expected: str = "",
    actual: str = "",
    key: str | None = None,
    target: str | None = None,
) -> ExploratoryFinding:
    return ExploratoryFinding(
        position=0,
        finding_type=finding_type,
        severity=severity,
        title=title,
        steps_to_reproduce="do the thing",
        expected=expected,
        actual=actual,
        tracker_issue_key=key,
        tracker_target=target,
    )


def _session(
    findings: list[ExploratoryFinding] | None = None,
    status: str = ExploratorySessionStatus.COMPLETED,
) -> ExploratorySession:
    exploratory_session = ExploratorySession(
        position=0,
        charter="Explore the thing",
        sfdipot_areas_csv="Function",
        status=status,
    )
    exploratory_session.findings = findings or []
    return exploratory_session


def _exploratory_run(
    requirement_id: int,
    sessions: list[ExploratorySession],
    status: str = ExploratoryRunStatus.COMPLETED,
) -> ExploratoryRun:
    run = ExploratoryRun(
        sprint_id=1,
        requirement_id=requirement_id,
        base_url_env_vars_csv="APP_URL",
        status=status,
    )
    run.sessions = sessions
    return run


def _requirement(
    requirement_id: int,
    name: str,
    *,
    archived: bool = False,
    status: str = RequirementStatus.CONFIRMED,
) -> Requirement:
    return Requirement(
        id=requirement_id,
        sprint_id=1,
        name=name,
        description="d",
        original_description="d",
        archived=archived,
        status=status,
    )


def _sprint(
    requirements: list[Requirement] | None = None,
    test_runs: list[TestRun] | None = None,
    exploratory_runs: list[ExploratoryRun] | None = None,
) -> Sprint:
    sprint = Sprint(id=1, name="Sprint 1", repo_id=1, directory="sprint-1")
    sprint.all_requirements = requirements or []
    sprint.test_runs = test_runs or []
    sprint.exploratory_runs = exploratory_runs or []
    return sprint


# ── the empty and near-empty cases ────────────────────────────────────


def test_empty_sprint_reports_zeros_and_null_densities():
    metrics = compute_sprint_metrics(_sprint())

    assert metrics["sprint_id"] == 1
    assert metrics["distinct_test_cases_run"] == 0
    assert metrics["case_executions"] == 0
    assert metrics["exploratory_sessions"] == 0
    assert metrics["bug_count"] == 0
    assert metrics["issue_count"] == 0
    assert metrics["requirements_covered"] == 0
    assert metrics["bugs_per_requirement"] is None
    assert metrics["bugs_per_test_case"] is None
    assert metrics["per_requirement"] == []


def test_exploratory_only_sprint_has_no_case_denominator():
    """Tested purely by exploration: one density is real, the other null."""
    sprint = _sprint(
        requirements=[_requirement(1, "Login")],
        exploratory_runs=[_exploratory_run(1, [_session([_finding("Login fails")])])],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["bug_count"] == 1
    assert metrics["distinct_test_cases_run"] == 0
    assert metrics["bugs_per_requirement"] == 1.0
    assert metrics["bugs_per_test_case"] is None


def test_scripted_only_sprint_with_no_bugs_reports_zero_not_null():
    """Tested and clean is a number, not an absence."""
    sprint = _sprint(
        requirements=[_requirement(1, "Login")],
        test_runs=[_run([_execution(1, [_case(10, TestCaseExecutionStatus.PASSED)])])],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["bug_count"] == 0
    assert metrics["bugs_per_test_case"] == 0.0
    assert metrics["bugs_per_requirement"] == 0.0


# ── defect collapse ───────────────────────────────────────────────────


def test_findings_sharing_a_ticket_collapse_across_two_runs():
    filed = {"key": "QA-1", "target": "jira:QA"}
    sprint = _sprint(
        requirements=[_requirement(1, "Checkout")],
        test_runs=[
            _run(
                [
                    _execution(
                        1,
                        [
                            _case(
                                10,
                                TestCaseExecutionStatus.FAILED,
                                title="Order not created",
                                **filed,
                            )
                        ],
                    )
                ]
            ),
            _run(
                [
                    _execution(
                        1,
                        [
                            _case(
                                11,
                                TestCaseExecutionStatus.FAILED,
                                title="Totally different wording",
                                **filed,
                            )
                        ],
                    )
                ]
            ),
        ],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["bug_count"] == 1
    assert metrics["distinct_test_cases_run"] == 2


def test_the_same_ticket_number_on_two_trackers_does_not_collapse():
    """GitHub issue numbers are per-repo integers — the pair, never the key."""
    sprint = _sprint(
        requirements=[_requirement(1, "Checkout")],
        test_runs=[
            _run(
                [
                    _execution(
                        1,
                        [
                            _case(
                                10,
                                TestCaseExecutionStatus.FAILED,
                                title="A",
                                key="7",
                                target="github:acme/a",
                            ),
                            _case(
                                11,
                                TestCaseExecutionStatus.FAILED,
                                title="B",
                                key="7",
                                target="github:acme/b",
                            ),
                        ],
                    )
                ]
            )
        ],
    )

    assert compute_sprint_metrics(sprint)["bug_count"] == 2


def test_identical_text_collapses_with_no_tracker_connected():
    """The clause that makes a tracker-free sprint count re-runs once."""
    sprint = _sprint(
        requirements=[_requirement(1, "Checkout")],
        test_runs=[
            _run(
                [
                    _execution(
                        1,
                        [
                            _case(
                                10,
                                TestCaseExecutionStatus.FAILED,
                                title="Order 8814 was not created",
                                expected="an order",
                                actual="none",
                            ),
                            _case(
                                11,
                                TestCaseExecutionStatus.FAILED,
                                title="Order 9021 was not created",
                                expected="an order",
                                actual="none",
                            ),
                        ],
                    )
                ]
            )
        ],
    )

    assert compute_sprint_metrics(sprint)["bug_count"] == 1


def test_ticket_identity_wins_over_text_when_both_apply():
    """Two identically-worded findings filed to different tickets stay two.

    The tracker recorded a decision an external system already made, which
    outranks the text looking the same.
    """
    sprint = _sprint(
        requirements=[_requirement(1, "Checkout")],
        test_runs=[
            _run(
                [
                    _execution(
                        1,
                        [
                            _case(
                                10,
                                TestCaseExecutionStatus.FAILED,
                                title="Order not created",
                                key="QA-1",
                                target="jira:QA",
                            ),
                            _case(
                                11,
                                TestCaseExecutionStatus.FAILED,
                                title="Order not created",
                                key="QA-2",
                                target="jira:QA",
                            ),
                        ],
                    )
                ]
            )
        ],
    )

    assert compute_sprint_metrics(sprint)["bug_count"] == 2


def test_differently_worded_findings_do_not_collapse():
    sprint = _sprint(
        requirements=[_requirement(1, "Checkout")],
        test_runs=[
            _run(
                [
                    _execution(
                        1,
                        [
                            _case(
                                10,
                                TestCaseExecutionStatus.FAILED,
                                title="Checkout returns 500",
                            ),
                            _case(
                                11,
                                TestCaseExecutionStatus.FAILED,
                                title="The order endpoint errors on submit",
                            ),
                        ],
                    )
                ]
            )
        ],
    )

    # Paraphrase grouping is the LLM stage, which stays behind the tracker
    # path — the metrics endpoint is polled, so it makes no LLM call.
    assert compute_sprint_metrics(sprint)["bug_count"] == 2


def test_group_severity_is_the_highest_among_its_members():
    filed = {"key": "QA-1", "target": "jira:QA"}
    sprint = _sprint(
        requirements=[_requirement(1, "Checkout")],
        test_runs=[
            _run(
                [
                    _execution(
                        1,
                        [
                            _case(
                                10,
                                TestCaseExecutionStatus.FAILED,
                                title="A",
                                severity="low",
                                **filed,
                            ),
                            _case(
                                11,
                                TestCaseExecutionStatus.FAILED,
                                title="B",
                                severity="high",
                                **filed,
                            ),
                        ],
                    )
                ]
            )
        ],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["bug_count"] == 1
    # Taking the first member's severity would let the high-severity report
    # hide behind the low-severity duplicate it was grouped with.
    assert metrics["high_severity_bug_count"] == 1


def test_issues_are_counted_raw_and_never_enter_the_bug_count():
    """Issues are deliberately *not* collapsed, unlike bugs.

    An issue says testing was obstructed, not that the product is wrong, so
    two cases blocked by the same unreachable environment are two pieces of
    testing that did not happen — collapsing them to one would understate
    how much of the run was lost.
    """
    sprint = _sprint(
        requirements=[_requirement(1, "Checkout")],
        test_runs=[
            _run(
                [
                    _execution(
                        1,
                        [
                            _case(
                                10,
                                TestCaseExecutionStatus.ERROR,
                                title="Environment unreachable",
                            ),
                            _case(
                                11,
                                TestCaseExecutionStatus.ERROR,
                                title="Environment unreachable",
                            ),
                        ],
                    )
                ]
            )
        ],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["issue_count"] == 2
    assert metrics["bug_count"] == 0
    # The scripted half of the same figure, in the same units.
    assert metrics["executions_errored"] == 2


def test_issue_rows_sum_to_the_issue_headline():
    """Ungrouped issues cannot diverge from their per-requirement rows.

    The bug headline legitimately reads *below* the sum of its rows, because
    one sprint-scoped group can span two requirements. Issues have no groups,
    so that gap is structurally impossible for them.
    """
    sprint = _sprint(
        requirements=[_requirement(1, "Login"), _requirement(2, "Checkout")],
        test_runs=[
            _run(
                [
                    _execution(
                        1, [_case(10, TestCaseExecutionStatus.ERROR, title="Env unreachable")]
                    ),
                    _execution(
                        2, [_case(20, TestCaseExecutionStatus.ERROR, title="Env unreachable")]
                    ),
                ]
            )
        ],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["issue_count"] == 2
    assert sum(row["issue_count"] for row in metrics["per_requirement"]) == 2


def test_a_bug_and_an_issue_with_the_same_text_stay_separate():
    """Identical text across the two types must never conflate them.

    ``dedup_key`` is type-blind, so the guarantee comes from ``_compute``
    partitioning by ``finding_type`` before either count is taken.
    """
    sprint = _sprint(
        requirements=[_requirement(1, "Checkout")],
        exploratory_runs=[
            _exploratory_run(
                1,
                [
                    _session(
                        [
                            _finding("Same words", finding_type="bug"),
                            _finding("Same words", finding_type="issue"),
                        ]
                    )
                ],
            )
        ],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["bug_count"] == 1
    assert metrics["issue_count"] == 1


# ── the two counting levels ───────────────────────────────────────────


def test_a_case_run_three_times_is_one_distinct_case_and_three_executions():
    runs = [_run([_execution(1, [_case(10, TestCaseExecutionStatus.PASSED)])]) for _ in range(3)]
    sprint = _sprint(requirements=[_requirement(1, "Login")], test_runs=runs)

    metrics = compute_sprint_metrics(sprint)

    assert metrics["distinct_test_cases_run"] == 1
    assert metrics["case_executions"] == 3
    assert metrics["executions_passed"] == 3


def test_re_running_an_unfixed_plan_does_not_move_bugs_per_test_case():
    """The regression the distinct-case denominator exists to prevent."""

    def plan_run() -> TestRun:
        return _run(
            [
                _execution(
                    1,
                    [
                        _case(10, TestCaseExecutionStatus.PASSED),
                        _case(
                            11,
                            TestCaseExecutionStatus.FAILED,
                            title="Order not created",
                            expected="an order",
                            actual="none",
                        ),
                    ],
                )
            ]
        )

    once = _sprint(requirements=[_requirement(1, "Checkout")], test_runs=[plan_run()])
    thrice = _sprint(
        requirements=[_requirement(1, "Checkout")],
        test_runs=[plan_run(), plan_run(), plan_run()],
    )

    first = compute_sprint_metrics(once)
    third = compute_sprint_metrics(thrice)

    assert first["bugs_per_test_case"] == 0.5
    assert third["bugs_per_test_case"] == 0.5
    # The execution level still records that testing happened three times.
    assert first["case_executions"] == 2
    assert third["case_executions"] == 6


def test_execution_statuses_split_three_ways():
    sprint = _sprint(
        requirements=[_requirement(1, "Login")],
        test_runs=[
            _run(
                [
                    _execution(
                        1,
                        [
                            _case(10, TestCaseExecutionStatus.PASSED),
                            _case(11, TestCaseExecutionStatus.FAILED, title="Bug"),
                            _case(12, TestCaseExecutionStatus.ERROR, title="Obstruction"),
                        ],
                    )
                ]
            )
        ],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["executions_passed"] == 1
    assert metrics["executions_failed"] == 1
    assert metrics["executions_errored"] == 1
    assert metrics["case_executions"] == 3


def test_a_skipped_case_in_a_completed_run_counts_at_neither_level():
    sprint = _sprint(
        requirements=[_requirement(1, "Login")],
        test_runs=[
            _run(
                [
                    _execution(
                        1,
                        [
                            _case(10, TestCaseExecutionStatus.PASSED),
                            _case(11, TestCaseExecutionStatus.SKIPPED),
                        ],
                    )
                ]
            )
        ],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["distinct_test_cases_run"] == 1
    assert metrics["case_executions"] == 1


# ── denominators and coverage ─────────────────────────────────────────


def test_density_divides_by_requirements_covered_not_by_the_sprint_total():
    """One of five features tested divides by one, and says so."""
    requirements = [_requirement(i, f"Feature {i}") for i in range(1, 6)]
    sprint = _sprint(
        requirements=requirements,
        test_runs=[
            _run(
                [
                    _execution(
                        1,
                        [
                            _case(10, TestCaseExecutionStatus.FAILED, title="A"),
                            _case(11, TestCaseExecutionStatus.FAILED, title="B"),
                        ],
                    )
                ]
            )
        ],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["requirements_covered"] == 1
    assert metrics["requirements_total"] == 5
    assert metrics["bugs_per_requirement"] == 2.0


def test_coverage_counts_both_modes_without_double_counting():
    sprint = _sprint(
        requirements=[_requirement(1, "Login"), _requirement(2, "Checkout")],
        test_runs=[_run([_execution(1, [_case(10, TestCaseExecutionStatus.PASSED)])])],
        exploratory_runs=[
            _exploratory_run(1, [_session()]),
            _exploratory_run(2, [_session()]),
        ],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["requirements_covered"] == 2
    assert metrics["requirements_explored"] == 2
    assert metrics["exploratory_sessions"] == 2


def test_a_skipped_session_does_not_count_but_an_errored_one_does():
    sprint = _sprint(
        requirements=[_requirement(1, "Login")],
        exploratory_runs=[
            _exploratory_run(
                1,
                [
                    _session(status=ExploratorySessionStatus.COMPLETED),
                    _session(status=ExploratorySessionStatus.ERROR),
                    _session(status=ExploratorySessionStatus.SKIPPED),
                ],
            )
        ],
    )

    assert compute_sprint_metrics(sprint)["exploratory_sessions"] == 2


# ── exclusions ────────────────────────────────────────────────────────


def test_unfinished_runs_are_excluded_counted_and_split_by_reason():
    in_flight = _run(
        [_execution(1, [_case(10, TestCaseExecutionStatus.PENDING)], TestExecutionStatus.RUNNING)]
    )
    broken = _run(
        [_execution(1, [_case(11, TestCaseExecutionStatus.SKIPPED)], TestExecutionStatus.FAILED)]
    )
    sprint = _sprint(
        requirements=[_requirement(1, "Login")],
        test_runs=[in_flight, broken],
        exploratory_runs=[_exploratory_run(1, [_session()], ExploratoryRunStatus.FAILED)],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["excluded_runs_running"] == 1
    assert metrics["excluded_runs_failed"] == 2
    # Nothing from an excluded run reaches the numbers.
    assert metrics["case_executions"] == 0
    assert metrics["exploratory_sessions"] == 0
    assert metrics["requirements_covered"] == 0


def test_a_failed_run_s_findings_are_not_counted():
    """An incomplete finding set is known to be incomplete."""
    sprint = _sprint(
        requirements=[_requirement(1, "Checkout")],
        test_runs=[
            _run(
                [
                    _execution(
                        1,
                        [_case(10, TestCaseExecutionStatus.FAILED, title="Order not created")],
                        TestExecutionStatus.FAILED,
                    )
                ]
            )
        ],
    )

    assert compute_sprint_metrics(sprint)["bug_count"] == 0


# ── the per-requirement breakdown ─────────────────────────────────────


def test_a_group_spanning_two_requirements_sums_above_the_headline():
    """Grouping is sprint-scoped: one dependency breaks two features."""
    filed = {"key": "QA-1", "target": "jira:QA"}
    sprint = _sprint(
        requirements=[_requirement(1, "Login"), _requirement(2, "Checkout")],
        test_runs=[
            _run(
                [
                    _execution(
                        1, [_case(10, TestCaseExecutionStatus.FAILED, title="Down", **filed)]
                    ),
                    _execution(
                        2, [_case(20, TestCaseExecutionStatus.FAILED, title="Down", **filed)]
                    ),
                ]
            )
        ],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["bug_count"] == 1
    assert sum(row["bug_count"] for row in metrics["per_requirement"]) == 2


def test_a_covered_requirement_with_no_findings_still_gets_a_row():
    """Tested and clean is a result worth showing."""
    sprint = _sprint(
        requirements=[_requirement(1, "Login")],
        test_runs=[_run([_execution(1, [_case(10, TestCaseExecutionStatus.PASSED)])])],
    )

    rows = compute_sprint_metrics(sprint)["per_requirement"]

    assert len(rows) == 1
    assert rows[0]["requirement_name"] == "Login"
    assert rows[0]["bug_count"] == 0
    assert rows[0]["distinct_test_cases_run"] == 1


def test_rows_are_ordered_by_bug_count_then_name():
    sprint = _sprint(
        requirements=[
            _requirement(1, "Zebra"),
            _requirement(2, "Alpha"),
            _requirement(3, "Worst"),
        ],
        test_runs=[
            _run(
                [
                    _execution(1, [_case(10, TestCaseExecutionStatus.PASSED)]),
                    _execution(2, [_case(20, TestCaseExecutionStatus.PASSED)]),
                    _execution(
                        3,
                        [
                            _case(30, TestCaseExecutionStatus.FAILED, title="A"),
                            _case(31, TestCaseExecutionStatus.FAILED, title="B"),
                        ],
                    ),
                ]
            )
        ],
    )

    rows = compute_sprint_metrics(sprint)["per_requirement"]

    assert [row["requirement_name"] for row in rows] == ["Worst", "Alpha", "Zebra"]


def test_an_archived_requirement_is_present_and_flagged():
    """Its bugs are in the headline, so hiding the row would not add up."""
    sprint = _sprint(
        requirements=[_requirement(1, "Deleted feature", archived=True)],
        test_runs=[
            _run([_execution(1, [_case(10, TestCaseExecutionStatus.FAILED, title="Broken")])])
        ],
    )

    metrics = compute_sprint_metrics(sprint)
    rows = metrics["per_requirement"]

    assert metrics["bug_count"] == 1
    assert len(rows) == 1
    assert rows[0]["requirement_name"] == "Deleted feature"
    assert rows[0]["requirement_deleted"] is True
    # It is excluded from the sprint's confirmed total but still covered.
    assert metrics["requirements_total"] == 0
    assert metrics["requirements_covered"] == 1


def test_a_requirement_covered_by_both_modes_reports_both_columns():
    sprint = _sprint(
        requirements=[_requirement(1, "Login")],
        test_runs=[_run([_execution(1, [_case(10, TestCaseExecutionStatus.PASSED)])])],
        exploratory_runs=[_exploratory_run(1, [_session(), _session()])],
    )

    rows = compute_sprint_metrics(sprint)["per_requirement"]

    assert len(rows) == 1
    assert rows[0]["distinct_test_cases_run"] == 1
    assert rows[0]["exploratory_sessions"] == 2


# ── agreement with the existing bug definition ────────────────────────


def test_the_bug_count_agrees_with_the_run_page_s_own_predicate():
    """``qa_metrics`` and ``TestRun.bug_findings`` must not drift.

    A titleless ``failed`` case is unreachable through live code, but it is
    exactly what would make the two disagree — the run page gating on
    ``finding_title`` while the panel counted by ``finding_type`` alone.
    """
    run = _run(
        [
            _execution(
                1,
                [
                    _case(10, TestCaseExecutionStatus.FAILED, title="Order not created"),
                    _case(11, TestCaseExecutionStatus.FAILED),  # no report written
                    _case(12, TestCaseExecutionStatus.PASSED),
                ],
            )
        ]
    )
    sprint = _sprint(requirements=[_requirement(1, "Checkout")], test_runs=[run])

    assert len(run.bug_findings) == 1
    assert compute_sprint_metrics(sprint)["bug_count"] == 1


# ── the never-raise contract ──────────────────────────────────────────


def test_a_broken_sprint_yields_zeros_rather_than_raising():
    """A metrics panel must not be able to 500 the page it decorates."""

    class Exploding:
        id = 7

        @property
        def test_runs(self):
            raise RuntimeError("boom")

    metrics = compute_sprint_metrics(Exploding())

    assert metrics["sprint_id"] == 7
    assert metrics["bug_count"] == 0
    assert metrics["bugs_per_requirement"] is None
