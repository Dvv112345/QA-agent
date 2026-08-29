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

* **One bug is one defect**, collapsed by the ``DefectGroup`` the
  assignment pass assigned at run completion, then by ticket, then by
  normalized text (``finding_dedup.dedup_key``, shared rather than
  reimplemented so the panel and the tracker cannot report different
  groupings of the same findings).  **Issues are never collapsed.**  An
  issue records that testing was obstructed, not that the product is wrong
  — the SBTM distinction — so "how many distinct defects" is not a question
  it answers.  Three cases erroring on the same unreachable environment are
  three pieces of testing that did not happen, and collapsing them to one
  would understate how much of the run was lost.  That also puts
  ``issue_count`` in the same units as ``executions_errored`` beside it,
  which is the scripted half of the same figure, rather than silently
  mixing a cumulative count with a distinct-defect one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.models.database import (
    DefectPool,
    ExploratoryRunStatus,
    ExploratorySessionStatus,
    FindingSeverity,
    FindingType,
    NonfunctionalChildStatus,
    NonfunctionalRunStatus,
    RequirementStatus,
    TestCaseExecutionStatus,
    TestExecutionStatus,
)
from backend.services.finding_dedup import dedup_key
from backend.services.findings import TERMINAL_CASE_STATUSES, Finding, iter_findings

logger = logging.getLogger(__name__)


# An errored session still drove a browser, so it counts as work done.
# `skipped` is the charter the run never reached.
_COUNTED_SESSION_STATUSES = frozenset(
    {
        ExploratorySessionStatus.COMPLETED,
        ExploratorySessionStatus.ERROR,
    }
)


def _defect_key(finding: Finding) -> tuple:
    """What makes two findings the same defect — three keys, in order.

    Uniformly tagged tuples, so three kinds of identity cannot collide in
    one dict.

    **The stored group outranks ticket identity.**  A ``DefectGroup`` is
    not text similarity: it is the same paraphrase-aware judgement, made
    once when the run completed and remembered, so it is the best answer
    available whenever it exists.

    What settles the order is the tracker switch.  Ticket identity is the
    **pair** ``(tracker_target, issue_key)`` — it has to be, since GitHub
    issue numbers are per-repo integers and repo B's ``#7`` would
    otherwise answer for repo A's — but that pair changes when the sprint
    is re-pointed at another tracker, so one defect found either side of a
    switch would count as two bugs.  ``tracker_target`` guards a *filing*
    hazard; letting it reach the headline fragments defect identity on a
    change that has nothing to do with the product.  Defect identity is a
    property of the product, ticket identity a property of where you
    happen to be filing, and the group is the only one of the three that
    expresses the first.

    The ticket branch survives beneath it for rows the assignment pass
    never grouped — a run that never completed, or a pass that fell over —
    where it is still the best evidence available.  ``dedup_key`` beneath
    that is what makes a sprint with no tracker and no grouping collapse
    its re-runs at all.
    """
    if finding.defect_group_id is not None:
        return ("group", finding.defect_group_id)
    if finding.tracker_issue_key:
        return ("ticket", finding.tracker_target or "", finding.tracker_issue_key)
    return ("text", dedup_key(finding.title, finding.expected, finding.actual))


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
    findings: list[Finding]
    scripted_requirements: set[int]
    explored_requirements: set[int]
    # requirement_id -> URLs a completed nonfunctional run examined
    urls_by_requirement: dict[int, set[str]]
    examined_requirements: set[int]
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
        urls_by_requirement={},
        examined_requirements=set(),
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
                if case.status not in TERMINAL_CASE_STATUSES:
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
            # Gated on a report as well as a type, exactly as
            # `TestRun.bug_findings` is — see `services/findings.py`. Counting
            # by `finding_type` alone would let the run page and this panel
            # disagree on the same sprint.
            counted.findings.extend(iter_findings(execution, terminal_only=True))

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
            counted.findings.extend(iter_findings(exploratory_session))

    for run in sprint.nonfunctional_runs:
        if run.status != NonfunctionalRunStatus.COMPLETED:
            if run.status == NonfunctionalRunStatus.FAILED:
                counted.excluded_failed += 1
            else:
                counted.excluded_running += 1
            continue
        # Counted like scripted cases rather than like exploratory
        # sessions: a URL examined twice across two runs is one thing
        # examined, and a run that re-walked the same feature has not
        # covered more of it.
        urls = counted.urls_by_requirement.setdefault(run.requirement_id, set())
        for target in run.targets:
            if target.status != NonfunctionalChildStatus.COMPLETED:
                continue
            urls.add(target.url)
        if urls:
            counted.examined_requirements.add(run.requirement_id)
        counted.findings.extend(iter_findings(run))

    return counted


def _group(findings: list[Finding]) -> list[list[Finding]]:
    """Collapse *findings* of one type into one list per distinct defect."""
    groups: dict[tuple, list[Finding]] = {}
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
    """All zeros — what a sprint with no completed run honestly reports.

    Also **the single declaration of the response shape**: `_compute`
    builds on top of this rather than re-listing every key.  The two used
    to be written out separately, which made a key added to one and
    forgotten in the other turn any metrics exception into a
    `response_model` validation error — a 500 on the one endpoint whose
    whole contract is that it can never take the page down.
    """
    return {
        "sprint_id": sprint_id,
        "distinct_test_cases_run": 0,
        "case_executions": 0,
        "executions_passed": 0,
        "executions_failed": 0,
        "executions_errored": 0,
        "exploratory_sessions": 0,
        "requirements_explored": 0,
        "urls_examined": 0,
        "requirements_examined": 0,
        "bug_count": 0,
        "functional_bug_count": 0,
        "nonfunctional_bug_count": 0,
        "bugs_by_domain": {},
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

    bugs = [f for f in counted.findings if f.finding_type == FindingType.BUG]
    bug_groups = _group(bugs)
    # Partitioned by pool, not re-derived from which list a row landed in:
    # `Finding.pool` comes from `findings.RunKind`, the same place
    # `finding_grouping` reads it, so the panel and the grouping pass cannot
    # disagree about what a nonfunctional defect is.
    functional_groups = [
        group for group in bug_groups if all(f.pool == DefectPool.FUNCTIONAL for f in group)
    ]
    nonfunctional_groups = [
        group for group in bug_groups if any(f.pool == DefectPool.NONFUNCTIONAL for f in group)
    ]
    # Issues are counted raw, never grouped — see `_compute`'s sibling rule in
    # the module docstring. An issue says testing was obstructed, not that the
    # product is wrong, so "how many distinct defects" is not a question it
    # answers.
    issue_findings = [f for f in counted.findings if f.finding_type == FindingType.ISSUE]

    distinct_cases = sum(len(cases) for cases in counted.cases_by_requirement.values())
    covered = (
        counted.scripted_requirements
        | counted.explored_requirements
        | counted.examined_requirements
    )

    bug_count = len(bug_groups)
    # The densities divide by the functional count alone (decision 12): a
    # nonfunctional run finds violations per *page*, not per test case, so
    # feeding them into "bugs per test case" would move a number whose
    # denominator never saw them. The headline still reports the total —
    # a real accessibility defect is a real defect.
    functional_bug_count = len(functional_groups)

    return {
        **_empty(sprint.id),
        "distinct_test_cases_run": distinct_cases,
        "case_executions": counted.case_executions,
        "executions_passed": counted.executions_passed,
        "executions_failed": counted.executions_failed,
        "executions_errored": counted.executions_errored,
        "exploratory_sessions": sum(counted.sessions_by_requirement.values()),
        "requirements_explored": len(counted.explored_requirements),
        "urls_examined": sum(len(urls) for urls in counted.urls_by_requirement.values()),
        "requirements_examined": len(counted.examined_requirements),
        "bug_count": bug_count,
        "functional_bug_count": functional_bug_count,
        "nonfunctional_bug_count": len(nonfunctional_groups),
        "bugs_by_domain": _bugs_by_domain(nonfunctional_groups),
        "issue_count": len(issue_findings),
        # A group is high-severity when *any* member reported it so — the
        # highest severity among them, mirroring how
        # `finding_dedup.elect_representative` picks the report that speaks
        # for a group. Taking the first member's would let a high-severity
        # defect hide behind a medium duplicate.
        "high_severity_bug_count": sum(
            1 for group in bug_groups if any(f.severity == FindingSeverity.HIGH for f in group)
        ),
        "requirements_covered": len(covered),
        # Confirmed live requirements — the sprint's testable feature set,
        # shown beside `requirements_covered` so coverage is legible. It is
        # a display figure only: nothing divides by it (see `_density`
        # below and the Decision it implements).
        #
        # It can read *below* `requirements_covered`, and legitimately: a
        # requirement covered by a run and then edited goes back to
        # `analyzing`, and an archived one leaves `sprint.requirements`
        # entirely, while the coverage it already contributed stands. The
        # two are therefore reported side by side and never as a fraction.
        "requirements_total": sum(
            1 for r in sprint.requirements if r.status == RequirementStatus.CONFIRMED
        ),
        "bugs_per_requirement": _density(functional_bug_count, len(covered)),
        "bugs_per_test_case": _density(functional_bug_count, distinct_cases),
        "per_requirement": _per_requirement(sprint, counted, bug_groups, issue_findings, covered),
        "excluded_runs_running": counted.excluded_running,
        "excluded_runs_failed": counted.excluded_failed,
    }


def _bugs_by_domain(nonfunctional_groups: list[list[Finding]]) -> dict[str, int]:
    """Distinct nonfunctional defects per domain.

    Keyed off the finding row rather than the normalized ``Finding``,
    because ``domain`` is the one field only this carrier has — the shared
    shape has nothing to say about it. A group is counted once per domain
    it touches, which is the same rule the per-requirement breakdown uses
    and for the same reason: a defect that spans two is genuinely in both.
    """
    counts: dict[str, int] = {}
    for group in nonfunctional_groups:
        for domain in {getattr(f.row, "domain", None) for f in group} - {None}:
            counts[domain] = counts.get(domain, 0) + 1
    return counts


def _per_requirement(
    sprint,
    counted: _Counted,
    bug_groups: list[list[Finding]],
    issue_findings: list[Finding],
    covered: set[int],
) -> list[dict]:
    """The breakdown, one row per requirement a counted run covered.

    A bug *group* is counted against **every** requirement it touches, so
    those rows can sum above the headline: grouping is sprint-scoped, and one
    broken dependency genuinely breaks login *and* checkout.  The rejected
    alternative was electing an owner requirement per group, which would
    make a requirement with real failures read as zero — a worse lie than a
    number needing one sentence of explanation.  The panel footnotes it,
    and only when the sums actually differ.

    Issue rows cannot diverge that way, because issues are not grouped: each
    finding belongs to exactly one requirement, so the rows sum to the
    headline exactly.  The footnote is therefore a bug-only concern.
    """
    labels = _requirement_labels(sprint)

    bugs_by_requirement: dict[int, int] = {}
    issues_by_requirement: dict[int, int] = {}
    for group in bug_groups:
        for requirement_id in {f.requirement_id for f in group}:
            bugs_by_requirement[requirement_id] = bugs_by_requirement.get(requirement_id, 0) + 1
    for finding in issue_findings:
        issues_by_requirement[finding.requirement_id] = (
            issues_by_requirement.get(finding.requirement_id, 0) + 1
        )

    rows: list[dict] = []
    for requirement_id in covered:
        # One lookup, one default. A covered id came from a counted run, so
        # it necessarily has a row in `all_requirements` and this default is
        # unreachable — it reads "deleted" so that the impossible case
        # understates rather than inventing a live requirement.
        name, deleted = labels.get(requirement_id, ("", True))
        rows.append(
            {
                "requirement_id": requirement_id,
                "requirement_name": name,
                "requirement_deleted": deleted,
                "bug_count": bugs_by_requirement.get(requirement_id, 0),
                "issue_count": issues_by_requirement.get(requirement_id, 0),
                "distinct_test_cases_run": len(
                    counted.cases_by_requirement.get(requirement_id, ())
                ),
                "exploratory_sessions": counted.sessions_by_requirement.get(requirement_id, 0),
            }
        )
    # Worst first — the reader's question is "which feature is carrying the
    # defects", and id order buries it.
    rows.sort(key=lambda row: (-row["bug_count"], row["requirement_name"]))
    return rows
