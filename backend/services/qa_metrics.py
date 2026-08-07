"""How QA went for one sprint, computed from rows that already exist.

Pure over its inputs and **never raises** — the ``finding_dedup`` /
``finding_export`` contract, for a reason of its own here: this feeds a
panel the test-runs page polls every 2.5 s, and a metrics panel must not
be able to 500 the page it decorates.  A count that comes back zero is a
worse answer than the truth; a page that will not load is not an answer
at all.

Nothing is stored, so there is no schema change and no migration — the
shape ``models/database.py::export_rollup`` already established for
response-time aggregation, one level up.

Three counting rules carry the whole module, and each exists to stop a
number moving for a reason that has nothing to do with testing:

* **Only completed runs count.**  Mirrors the export rule exactly (*a run
  that finished reports; anything else waits for a human*).  An aborted or
  in-flight run's finding set is incomplete **and known to be
  incomplete**, and its case denominator under-counts because the cases it
  never reached never ran.  Excluded runs are counted and named rather
  than silently dropped.

* **Scripted cases are counted at two levels that never mix.**
  ``distinct_test_cases_run`` is the density denominator;
  ``case_executions`` and its status split describe how much testing was
  done.  Dividing by executions would make a sprint read three times
  healthier for re-running an unfixed plan — a metric that rewards noise.

* **One bug is one defect**, collapsed by ticket where one was filed and
  by normalized text otherwise (``finding_dedup.dedup_key``, shared rather
  than reimplemented so the panel and the tracker cannot report different
  groupings of the same findings).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.models.database import (
    ExploratoryRunStatus,
    ExploratorySessionStatus,
    FindingSeverity,
    FindingType,
    RequirementStatus,
    TestCaseExecutionStatus,
    TestExecutionStatus,
)
from backend.services.finding_dedup import dedup_key

logger = logging.getLogger(__name__)

# A case that reached a verdict. Defensive given `abandon_unreached_children`
# — a completed run should hold no `pending`/`running`/`skipped` case — but
# the filter is what makes that guarantee locally visible instead of assumed.
_TERMINAL_CASE_STATUSES = frozenset(
    {
        TestCaseExecutionStatus.PASSED,
        TestCaseExecutionStatus.FAILED,
        TestCaseExecutionStatus.ERROR,
    }
)

# An errored session still drove a browser, so it counts as work done.
# `skipped` is the charter the run never reached.
_COUNTED_SESSION_STATUSES = frozenset(
    {
        ExploratorySessionStatus.COMPLETED,
        ExploratorySessionStatus.ERROR,
    }
)


@dataclass(frozen=True)
class _Finding:
    """One finding off either carrier, under one set of names.

    ``TestCaseExecution`` prefixes its fields ``finding_`` while
    ``ExploratoryFinding`` uses the bare names, so both are read into this
    shape once and everything below is carrier-agnostic — the same
    adaptation ``finding_export`` does into ``FindingCandidate``.  Needed
    regardless of de-duplication, since the per-requirement bucketing walks
    both carriers too.
    """

    finding_type: str
    severity: str
    title: str
    expected: str
    actual: str
    tracker_issue_key: str | None
    tracker_target: str | None
    requirement_id: int


def _defect_key(finding: _Finding) -> tuple[str, str] | str:
    """What makes two findings the same defect.

    Ticket identity wins where it exists: it records a decision an external
    system already made, which is stronger evidence than text similarity.
    That precedence mirrors ``finding_dedup._merge_with_llm``, where a
    prefilter match beats the model's judgement for the same reason.

    The **pair**, never the bare key: GitHub issue numbers are per-repo
    integers, so without ``tracker_target`` a sprint whose tracker was
    switched mid-way would merge repo B's ``#7`` into repo A's.

    Falling back to ``dedup_key`` rather than to row identity is what makes
    a sprint with no tracker connected collapse its re-runs at all — one
    broken dependency fails every case in a plan with the same words, which
    that function's own module calls the common case.
    """
    if finding.tracker_issue_key:
        return (finding.tracker_target or "", finding.tracker_issue_key)
    return dedup_key(finding.title, finding.expected, finding.actual)


def _scripted_findings(execution) -> list[_Finding]:
    """This execution's findings, in case order.

    **Gated on ``finding_title``**, exactly as ``TestRun.bug_findings`` is.
    Not for legacy rows — live code writes the finding fields as a group on
    the terminal-failure path, so a titleless verdict is unreachable — but
    because ``bug_findings`` is what ``export_rollup`` counts and what the
    run pages display.  Counting by ``finding_type`` alone here would let
    the two definitions disagree on the same sprint: the run page reporting
    three bugs beside a metrics panel reporting four, with nothing to say
    which is right.  One condition removes the possibility.
    """
    findings: list[_Finding] = []
    for case in execution.cases:
        if case.status not in _TERMINAL_CASE_STATUSES:
            continue
        if not case.finding_type or not case.finding_title:
            continue
        findings.append(
            _Finding(
                finding_type=case.finding_type,
                severity=case.finding_severity or "",
                title=case.finding_title,
                expected=case.finding_expected or "",
                actual=case.finding_actual or "",
                tracker_issue_key=case.tracker_issue_key,
                tracker_target=case.tracker_target,
                requirement_id=execution.requirement_id,
            )
        )
    return findings


def _exploratory_findings(run, exploratory_session) -> list[_Finding]:
    """One session's findings, in position order.

    No title gate: ``ExploratoryFinding.title`` is a non-nullable column
    written by the ``record_finding`` tool, so there is no titleless row to
    guard against — the asymmetry with the scripted side is in the schema,
    not in the counting rule.
    """
    return [
        _Finding(
            finding_type=finding.finding_type,
            severity=finding.severity,
            title=finding.title,
            expected=finding.expected,
            actual=finding.actual,
            tracker_issue_key=finding.tracker_issue_key,
            tracker_target=finding.tracker_target,
            requirement_id=run.requirement_id,
        )
        for finding in exploratory_session.findings
    ]


@dataclass
class _Counted:
    """Everything one pass over the sprint's completed runs collected."""

    # requirement_id -> distinct test_case_id values with a terminal execution
    cases_by_requirement: dict[int, set[int]]
    case_executions: int
    executions_passed: int
    executions_failed: int
    executions_errored: int
    # requirement_id -> counted session count
    sessions_by_requirement: dict[int, int]
    findings: list[_Finding]
    scripted_requirements: set[int]
    explored_requirements: set[int]
    excluded_running: int
    excluded_failed: int


def _collect(sprint) -> _Counted:
    """Walk both run chains once, keeping only what completed runs produced."""
    counted = _Counted(
        cases_by_requirement={},
        case_executions=0,
        executions_passed=0,
        executions_failed=0,
        executions_errored=0,
        sessions_by_requirement={},
        findings=[],
        scripted_requirements=set(),
        explored_requirements=set(),
        excluded_running=0,
        excluded_failed=0,
    )

    for run in sprint.test_runs:
        # `TestRun.status` is rolled up from its executions, so this is the
        # same "did it finish" question the run page answers.
        if run.status != TestExecutionStatus.COMPLETED:
            if run.status == TestExecutionStatus.FAILED:
                counted.excluded_failed += 1
            else:
                counted.excluded_running += 1
            continue
        for execution in run.executions:
            requirement_id = execution.requirement_id
            cases = counted.cases_by_requirement.setdefault(requirement_id, set())
            for case in execution.cases:
                if case.status not in _TERMINAL_CASE_STATUSES:
                    continue
                cases.add(case.test_case_id)
                counted.case_executions += 1
                if case.status == TestCaseExecutionStatus.PASSED:
                    counted.executions_passed += 1
                elif case.status == TestCaseExecutionStatus.FAILED:
                    counted.executions_failed += 1
                else:
                    counted.executions_errored += 1
            # Covered means "a counted run touched it", which an execution
            # whose every case was skipped did not. The empty set stays in
            # the map rather than being popped — a later execution of the
            # same requirement writes into it, and popping would discard
            # what an earlier one already counted.
            if cases:
                counted.scripted_requirements.add(requirement_id)
            counted.findings.extend(_scripted_findings(execution))

    for run in sprint.exploratory_runs:
        if run.status != ExploratoryRunStatus.COMPLETED:
            if run.status == ExploratoryRunStatus.FAILED:
                counted.excluded_failed += 1
            else:
                counted.excluded_running += 1
            continue
        for exploratory_session in run.sessions:
            if exploratory_session.status not in _COUNTED_SESSION_STATUSES:
                continue
            counted.sessions_by_requirement[run.requirement_id] = (
                counted.sessions_by_requirement.get(run.requirement_id, 0) + 1
            )
            counted.explored_requirements.add(run.requirement_id)
            counted.findings.extend(_exploratory_findings(run, exploratory_session))

    return counted


def _group(findings: list[_Finding]) -> list[list[_Finding]]:
    """Collapse *findings* of one type into one list per distinct defect."""
    groups: dict[tuple[str, str] | str, list[_Finding]] = {}
    for finding in findings:
        groups.setdefault(_defect_key(finding), []).append(finding)
    return list(groups.values())


def _requirement_labels(sprint) -> dict[int, tuple[str, bool]]:
    """``requirement_id -> (name, deleted)`` over archived rows too.

    Read from ``all_requirements``, deliberately: a run that executed
    against a since-deleted requirement still contributed its findings to
    the headline, so the breakdown has to be able to name it.
    """
    return {r.id: (r.name, bool(r.archived)) for r in sprint.all_requirements}


def _density(numerator: int, denominator: int) -> float | None:
    """The ratio, or None when nothing was exercised.

    Null rather than zero so the panel can say "nothing tested" and
    "tested and clean" differently, and so no TSX needs a divide guard.
    """
    return numerator / denominator if denominator else None


def compute_sprint_metrics(sprint) -> dict:
    """This sprint's QA metrics, ready to splat into the response model."""
    try:
        return _compute(sprint)
    except Exception:
        sprint_id = getattr(sprint, "id", None)
        logger.exception("Computing QA metrics failed for sprint id=%s", sprint_id)
        return _empty(sprint_id or 0)


def _empty(sprint_id: int) -> dict:
    """All zeros — what a sprint with no completed run honestly reports."""
    return {
        "sprint_id": sprint_id,
        "distinct_test_cases_run": 0,
        "case_executions": 0,
        "executions_passed": 0,
        "executions_failed": 0,
        "executions_errored": 0,
        "exploratory_sessions": 0,
        "requirements_explored": 0,
        "bug_count": 0,
        "issue_count": 0,
        "high_severity_bug_count": 0,
        "requirements_covered": 0,
        "requirements_total": 0,
        "bugs_per_requirement": None,
        "bugs_per_test_case": None,
        "per_requirement": [],
        "excluded_runs_running": 0,
        "excluded_runs_failed": 0,
    }


def _compute(sprint) -> dict:
    counted = _collect(sprint)

    bug_groups = _group([f for f in counted.findings if f.finding_type == FindingType.BUG])
    issue_groups = _group([f for f in counted.findings if f.finding_type == FindingType.ISSUE])

    distinct_cases = sum(len(cases) for cases in counted.cases_by_requirement.values())
    covered = counted.scripted_requirements | counted.explored_requirements

    bug_count = len(bug_groups)

    return {
        "sprint_id": sprint.id,
        "distinct_test_cases_run": distinct_cases,
        "case_executions": counted.case_executions,
        "executions_passed": counted.executions_passed,
        "executions_failed": counted.executions_failed,
        "executions_errored": counted.executions_errored,
        "exploratory_sessions": sum(counted.sessions_by_requirement.values()),
        "requirements_explored": len(counted.explored_requirements),
        "bug_count": bug_count,
        "issue_count": len(issue_groups),
        # A group is high-severity when *any* member reported it so — the
        # highest severity among them, mirroring how `finding_dedup._elect`
        # picks the representative whose report becomes the ticket. Taking
        # the first member's would let a high-severity defect hide behind a
        # medium duplicate.
        "high_severity_bug_count": sum(
            1 for group in bug_groups if any(f.severity == FindingSeverity.HIGH for f in group)
        ),
        "requirements_covered": len(covered),
        # Confirmed live requirements — the sprint's testable feature set,
        # shown beside `requirements_covered` so coverage is legible. It is
        # a display figure only: nothing divides by it (see `_density`
        # below and the Decision it implements).
        "requirements_total": sum(
            1 for r in sprint.requirements if r.status == RequirementStatus.CONFIRMED
        ),
        "bugs_per_requirement": _density(bug_count, len(covered)),
        "bugs_per_test_case": _density(bug_count, distinct_cases),
        "per_requirement": _per_requirement(sprint, counted, bug_groups, issue_groups),
        "excluded_runs_running": counted.excluded_running,
        "excluded_runs_failed": counted.excluded_failed,
    }


def _per_requirement(
    sprint,
    counted: _Counted,
    bug_groups: list[list[_Finding]],
    issue_groups: list[list[_Finding]],
) -> list[dict]:
    """The breakdown, one row per requirement a counted run covered.

    A group is counted against **every** requirement it touches, so the
    rows can sum above the headline: grouping is sprint-scoped, and one
    broken dependency genuinely breaks login *and* checkout.  The rejected
    alternative was electing an owner requirement per group, which would
    make a requirement with real failures read as zero — a worse lie than a
    number needing one sentence of explanation.  The panel footnotes it,
    and only when the sums actually differ.
    """
    labels = _requirement_labels(sprint)
    covered = counted.scripted_requirements | counted.explored_requirements

    bugs_by_requirement: dict[int, int] = {}
    issues_by_requirement: dict[int, int] = {}
    for groups, tally in ((bug_groups, bugs_by_requirement), (issue_groups, issues_by_requirement)):
        for group in groups:
            for requirement_id in {f.requirement_id for f in group}:
                tally[requirement_id] = tally.get(requirement_id, 0) + 1

    rows = [
        {
            "requirement_id": requirement_id,
            "requirement_name": labels.get(requirement_id, ("", False))[0],
            "requirement_deleted": labels.get(requirement_id, ("", True))[1],
            "bug_count": bugs_by_requirement.get(requirement_id, 0),
            "issue_count": issues_by_requirement.get(requirement_id, 0),
            "distinct_test_cases_run": len(counted.cases_by_requirement.get(requirement_id, ())),
            "exploratory_sessions": counted.sessions_by_requirement.get(requirement_id, 0),
        }
        for requirement_id in covered
    ]
    # Worst first — the reader's question is "which feature is carrying the
    # defects", and id order buries it.
    rows.sort(key=lambda row: (-row["bug_count"], row["requirement_name"]))
    return rows
