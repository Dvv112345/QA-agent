"""Unit tests for the QA metrics aggregator.

Built from real model instances but no database: the models are ordinary
Python objects until something persists them, and relationship attributes
work on transient instances.  That is deliberate rather than convenient —
it gives the tests the *derived* properties (``TestRun.status``,
``TestCaseExecution.finding_type``) for free, so the counting rules are
pinned against the same definitions production reads instead of against a
fake's restatement of them.
"""

import pytest

from backend.models.database import (
    ExploratoryFinding,
    ExploratoryRun,
    ExploratoryRunStatus,
    ExploratorySession,
    ExploratorySessionStatus,
    NonfunctionalChildStatus,
    NonfunctionalFinding,
    NonfunctionalRun,
    NonfunctionalRunStatus,
    NonfunctionalTarget,
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
    group: int | None = None,
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
        defect_group_id=group,
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
    group: int | None = None,
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
        defect_group_id=group,
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
    nonfunctional_runs: list["NonfunctionalRun"] | None = None,
) -> Sprint:
    sprint = Sprint(id=1, name="Sprint 1", repo_id=1, directory="sprint-1")
    sprint.all_requirements = requirements or []
    sprint.test_runs = test_runs or []
    sprint.exploratory_runs = exploratory_runs or []
    sprint.nonfunctional_runs = nonfunctional_runs or []
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

    # This endpoint makes no LLM call, so paraphrases it was never told
    # about stay apart. What closes that gap is `defect_group_id`, written
    # by the assignment pass when the run completed — see below.
    assert compute_sprint_metrics(sprint)["bug_count"] == 2


def test_a_shared_defect_group_collapses_findings_with_nothing_else_in_common():
    """The paraphrase case, answered by a column instead of a call.

    Different wording, no ticket, two requirements — one defect, because
    ``finding_grouping`` said so at completion and wrote it down.
    """
    sprint = _sprint(
        requirements=[_requirement(1, "Checkout"), _requirement(2, "Orders")],
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
                                group=7,
                            )
                        ],
                    ),
                    _execution(
                        2,
                        [
                            _case(
                                11,
                                TestCaseExecutionStatus.FAILED,
                                title="The order endpoint errors on submit",
                                group=7,
                            )
                        ],
                    ),
                ]
            )
        ],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["bug_count"] == 1
    # Grouping is sprint-scoped, so one broken dependency genuinely breaks
    # two features — both rows carry it, and they sum above the headline.
    assert [row["bug_count"] for row in metrics["per_requirement"]] == [1, 1]


def test_one_group_filed_under_two_tickets_is_still_one_bug():
    """The group outranks ticket identity, and this is why.

    Filing is idempotent per tracker, not per defect: a re-run whose
    filing failed and was retried, or a group re-elected after an error,
    can leave two keys on one defect. Counting keys would report two.
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
                                group=7,
                            ),
                            _case(
                                11,
                                TestCaseExecutionStatus.FAILED,
                                title="Order not created",
                                key="QA-2",
                                target="jira:QA",
                                group=7,
                            ),
                        ],
                    )
                ]
            )
        ],
    )

    assert compute_sprint_metrics(sprint)["bug_count"] == 1


def test_a_defect_found_either_side_of_a_tracker_switch_is_one_bug():
    """The case that settles the precedence.

    Ticket identity is the ``(target, key)`` **pair**, so ticket-first
    would count this defect twice for a reason that has nothing to do with
    the product — somebody re-pointed the sprint at another tracker.
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
                                group=7,
                            ),
                            _case(
                                11,
                                TestCaseExecutionStatus.FAILED,
                                title="Order not created",
                                key="7",
                                target="github:acme/shop",
                                group=7,
                            ),
                        ],
                    )
                ]
            )
        ],
    )

    assert compute_sprint_metrics(sprint)["bug_count"] == 1


def test_an_ungrouped_finding_falls_back_to_ticket_then_to_text():
    """Today's behaviour, unchanged, for rows the pass never reached.

    Three findings: one grouped, one ungrouped but filed, one with
    neither — and the ungrouped pair share text but not a ticket, so they
    stay apart on ticket identity exactly as before.
    """
    sprint = _sprint(
        requirements=[_requirement(1, "Checkout")],
        exploratory_runs=[
            _exploratory_run(
                1,
                [
                    _session(
                        [
                            _finding("Grouped", group=7),
                            _finding("Same words", key="QA-9", target="jira:QA"),
                            _finding("Same words"),
                        ]
                    )
                ],
            )
        ],
    )

    assert compute_sprint_metrics(sprint)["bug_count"] == 3


def test_group_severity_is_the_highest_among_a_grouped_pair():
    """Same rule as the ticket-grouped case, over the new key."""
    sprint = _sprint(
        requirements=[_requirement(1, "Checkout")],
        exploratory_runs=[
            _exploratory_run(
                1,
                [
                    _session(
                        [
                            _finding("A", severity="low", group=7),
                            _finding("B", severity="high", group=7),
                        ]
                    )
                ],
            )
        ],
    )

    metrics = compute_sprint_metrics(sprint)

    assert metrics["bug_count"] == 1
    assert metrics["high_severity_bug_count"] == 1


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


# ── Nonfunctional runs ════════════════════════════════════════════════


def _nf_finding(
    title: str,
    *,
    rule: str = "image-alt",
    domain: str = "accessibility",
    finding_type: str = "bug",
    severity: str = "medium",
    group: int | None = None,
) -> NonfunctionalFinding:
    return NonfunctionalFinding(
        position=0,
        domain=domain,
        rule=rule,
        finding_type=finding_type,
        severity=severity,
        title=title,
        steps_to_reproduce="open the page",
        expected="e",
        actual="a",
        defect_group_id=group,
    )


def _target(
    findings: list[NonfunctionalFinding] | None = None,
    *,
    url: str = "https://app.test/login",
    status: str = NonfunctionalChildStatus.COMPLETED,
) -> NonfunctionalTarget:
    target = NonfunctionalTarget(position=0, url=url, status=status)
    target.findings = findings or []
    return target


def _nonfunctional_run(
    requirement_id: int,
    targets: list[NonfunctionalTarget],
    status: str = NonfunctionalRunStatus.COMPLETED,
) -> NonfunctionalRun:
    run = NonfunctionalRun(
        sprint_id=1,
        requirement_id=requirement_id,
        base_url_env_vars_csv="APP_URL",
        domains_csv="accessibility,security,performance",
        status=status,
    )
    run.targets = targets
    return run


class TestNonfunctionalMetrics:
    """The third run mode: in the headline, out of both densities."""

    def _mixed_sprint(self, nonfunctional_findings):
        return _sprint(
            requirements=[_requirement(1, "Login")],
            test_runs=[
                _run(
                    [
                        _execution(
                            1,
                            [
                                _case(
                                    10,
                                    TestCaseExecutionStatus.FAILED,
                                    title="Login rejects a valid password",
                                )
                            ],
                        )
                    ]
                )
            ],
            nonfunctional_runs=[_nonfunctional_run(1, [_target(nonfunctional_findings)])],
        )

    def test_the_headline_holds_both_and_the_halves_add_up(self):
        sprint = self._mixed_sprint(
            [
                _nf_finding("Images lack alt text", rule="image-alt"),
                _nf_finding("No CSP", rule="missing-csp", domain="security"),
            ]
        )

        metrics = compute_sprint_metrics(sprint)

        assert metrics["bug_count"] == 3
        assert metrics["functional_bug_count"] == 1
        assert metrics["nonfunctional_bug_count"] == 2
        assert (
            metrics["functional_bug_count"] + metrics["nonfunctional_bug_count"]
            == metrics["bug_count"]
        )

    def test_both_densities_exclude_nonfunctional_bugs(self):
        sprint = self._mixed_sprint(
            [_nf_finding(f"Violation {n}", rule=f"rule-{n}") for n in range(5)]
        )

        metrics = compute_sprint_metrics(sprint)

        # One functional bug over one distinct case and one covered
        # requirement — the five nonfunctional ones move neither.
        assert metrics["bugs_per_test_case"] == 1.0
        assert metrics["bugs_per_requirement"] == 1.0

    def test_urls_examined_is_counted_and_never_summed_with_cases(self):
        sprint = self._mixed_sprint([_nf_finding("Images lack alt text")])

        metrics = compute_sprint_metrics(sprint)

        assert metrics["urls_examined"] == 1
        assert metrics["requirements_examined"] == 1
        assert metrics["distinct_test_cases_run"] == 1

    def test_the_domain_breakdown_names_what_was_found(self):
        sprint = self._mixed_sprint(
            [
                _nf_finding("A", rule="image-alt"),
                _nf_finding("B", rule="label"),
                _nf_finding("C", rule="missing-csp", domain="security"),
            ]
        )

        metrics = compute_sprint_metrics(sprint)

        assert metrics["bugs_by_domain"] == {"accessibility": 2, "security": 1}

    def test_a_nonfunctional_issue_is_never_collapsed_into_the_bug_count(self):
        sprint = _sprint(
            requirements=[_requirement(1, "Login")],
            nonfunctional_runs=[
                _nonfunctional_run(1, [_target([_nf_finding("Blocked", finding_type="issue")])])
            ],
        )

        metrics = compute_sprint_metrics(sprint)

        assert metrics["bug_count"] == 0
        assert metrics["issue_count"] == 1

    def test_a_shared_defect_group_collapses_across_run_modes(self):
        """Group precedence is what stops a re-run inflating the count."""
        sprint = self._mixed_sprint(
            [_nf_finding("A", group=7), _nf_finding("B", rule="label", group=7)]
        )

        metrics = compute_sprint_metrics(sprint)

        assert metrics["nonfunctional_bug_count"] == 1
        assert metrics["bug_count"] == 2

    @pytest.mark.parametrize(
        ("status", "field"),
        [
            (NonfunctionalRunStatus.RUNNING, "excluded_runs_running"),
            (NonfunctionalRunStatus.FAILED, "excluded_runs_failed"),
            (NonfunctionalRunStatus.PENDING, "excluded_runs_running"),
        ],
    )
    def test_a_non_completed_run_is_excluded_and_named(self, status, field):
        sprint = _sprint(
            requirements=[_requirement(1, "Login")],
            nonfunctional_runs=[
                _nonfunctional_run(1, [_target([_nf_finding("A")])], status=status)
            ],
        )

        metrics = compute_sprint_metrics(sprint)

        assert metrics["bug_count"] == 0
        assert metrics[field] == 1

    def test_only_completed_targets_count_toward_coverage(self):
        sprint = _sprint(
            requirements=[_requirement(1, "Login")],
            nonfunctional_runs=[
                _nonfunctional_run(
                    1,
                    [
                        _target([], url="https://app.test/a"),
                        _target(
                            [],
                            url="https://app.test/b",
                            status=NonfunctionalChildStatus.SKIPPED,
                        ),
                    ],
                )
            ],
        )

        assert compute_sprint_metrics(sprint)["urls_examined"] == 1

    def test_a_url_examined_by_two_runs_is_one_url(self):
        sprint = _sprint(
            requirements=[_requirement(1, "Login")],
            nonfunctional_runs=[
                _nonfunctional_run(1, [_target([])]),
                _nonfunctional_run(1, [_target([])]),
            ],
        )

        assert compute_sprint_metrics(sprint)["urls_examined"] == 1

    def test_a_nonfunctional_only_sprint_reports_a_null_case_density(self):
        sprint = _sprint(
            requirements=[_requirement(1, "Login")],
            nonfunctional_runs=[_nonfunctional_run(1, [_target([_nf_finding("A")])])],
        )

        metrics = compute_sprint_metrics(sprint)

        assert metrics["bug_count"] == 1
        assert metrics["requirements_covered"] == 1
        # Nothing functional was found, and the nonfunctional bug is not a
        # numerator here — both densities read zero rather than one.
        assert metrics["bugs_per_requirement"] == 0.0
        assert metrics["bugs_per_test_case"] is None

    def test_a_thrown_exception_still_returns_the_empty_shape(self, monkeypatch):
        import backend.services.qa_metrics as metrics_module

        sprint = self._mixed_sprint([_nf_finding("A")])
        monkeypatch.setattr(
            metrics_module,
            "_collect",
            lambda _sprint: (_ for _ in ()).throw(RuntimeError("row is nonsense")),
        )

        metrics = compute_sprint_metrics(sprint)

        assert metrics["bug_count"] == 0
        assert metrics["functional_bug_count"] == 0
        assert metrics["bugs_by_domain"] == {}
        assert metrics["sprint_id"] == 1
