"""One walk over a parent's findings, and one definition of what counts.

Findings live on two carriers that store the same observation under
different names: ``TestCaseExecution`` prefixes its fields ``finding_`` and
derives ``finding_type`` from the case status, while ``ExploratoryFinding``
uses bare names and stores its type.  Five places needed the same walk —
the run pages' ``bug_findings``, the export, the grouping pass, and the
metrics panel — and each grew its own copy of the traversal, the
column-name mapping, and the gate.

Each copy carried a docstring warning that it must not drift from the
others, which is the tell: the count on the run page, the count in the
panel, and the number of tickets filed are three renderings of one answer,
and a missed edit made them disagree with nothing to say which was right.
So the answer is computed here, once, and each caller projects the
normalized shape into whatever its own layer wants.

**The gate is the important part.**  A finding counts when it has a type
*and* a report to show.  ``finding_type`` is derived from status alone on
the scripted side, so a case can read as ``bug`` while carrying no report;
that row must not reach a bug count the user sees, and must never reach the
exporter, which would file an empty ticket into a system this application
cannot take back.  The exploratory side has no such hole — ``title`` is
non-nullable there — so ``has_report`` is simply always true for it.  The
asymmetry is in the schema, not in the counting rule.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from backend.models.database import (
    ExploratoryRun,
    ExploratorySession,
    FindingType,
    Sprint,
    TestCaseExecutionStatus,
    TestExecution,
    TestRun,
)

logger = logging.getLogger(__name__)

# A case that reached a verdict.  Defensive given
# `abandon_unreached_children` — a completed run should hold no
# `pending`/`running`/`skipped` case — but the filter is what makes that
# guarantee locally visible instead of assumed.
TERMINAL_CASE_STATUSES = frozenset(
    {
        TestCaseExecutionStatus.PASSED,
        TestCaseExecutionStatus.FAILED,
        TestCaseExecutionStatus.ERROR,
    }
)


@dataclass(frozen=True)
class Finding:
    """One finding off either carrier, under one set of names.

    The superset of what the callers need, so each can pick its own
    subset rather than re-reading the row.  ``row`` is the carrier itself,
    which is what a caller writes a tracker receipt or a group id back to.
    """

    row: Any  # TestCaseExecution | ExploratoryFinding
    finding_type: str | None
    severity: str
    title: str
    steps_to_reproduce: str
    expected: str
    actual: str
    environment: str | None
    tracker_issue_key: str | None
    tracker_target: str | None
    defect_group_id: int | None
    screenshot_path: str | None
    source_label: str  # test-case title | charter text
    requirement_id: int | None
    # Whether the work that produced this finding reached a verdict.
    # Always true on the exploratory side: a finding exists only because
    # the model recorded one.
    terminal: bool


def _from_case(case, requirement_id: int | None) -> Finding:
    return Finding(
        row=case,
        finding_type=case.finding_type,
        severity=case.finding_severity or "",
        title=case.finding_title or "",
        steps_to_reproduce=case.finding_steps_to_reproduce or "",
        expected=case.finding_expected or "",
        actual=case.finding_actual or "",
        environment=case.environment,
        tracker_issue_key=case.tracker_issue_key,
        tracker_target=case.tracker_target,
        defect_group_id=case.defect_group_id,
        screenshot_path=None,  # scripted runs capture no screenshot
        source_label=case.test_case.title if case.test_case else "",
        requirement_id=requirement_id,
        terminal=case.status in TERMINAL_CASE_STATUSES,
    )


def _from_exploratory(finding, charter: str, requirement_id: int | None) -> Finding:
    return Finding(
        row=finding,
        finding_type=finding.finding_type,
        severity=finding.severity,
        title=finding.title,
        steps_to_reproduce=finding.steps_to_reproduce,
        expected=finding.expected,
        actual=finding.actual,
        environment=finding.environment,
        tracker_issue_key=finding.tracker_issue_key,
        tracker_target=finding.tracker_target,
        defect_group_id=finding.defect_group_id,
        screenshot_path=finding.screenshot_path,
        source_label=charter,
        requirement_id=requirement_id,
        terminal=True,
    )


def _walk(parent: object) -> Iterator[Finding]:
    """Every finding under *parent*, in the order the run produced them.

    Accepts either level of either carrier's hierarchy, because the
    callers sit at different levels: the run pages hold a whole run, the
    export and grouping passes hold the unit one job owns, and the
    metrics panel walks sessions one at a time.
    """
    if isinstance(parent, TestRun):
        for execution in parent.executions:
            yield from _walk(execution)
    elif isinstance(parent, TestExecution):
        for case in parent.cases:
            yield _from_case(case, parent.requirement_id)
    elif isinstance(parent, ExploratoryRun):
        for exploratory_session in parent.sessions:
            for finding in exploratory_session.findings:
                yield _from_exploratory(finding, exploratory_session.charter, parent.requirement_id)
    elif isinstance(parent, ExploratorySession):
        run = parent.exploratory_run
        requirement_id = run.requirement_id if run is not None else None
        for finding in parent.findings:
            yield _from_exploratory(finding, parent.charter, requirement_id)
    else:
        logger.warning("Cannot read findings from %r — unknown parent type", type(parent))


def iter_findings(
    parent: object,
    *,
    bugs_only: bool = False,
    ungrouped_only: bool = False,
    unfiled_only: bool = False,
    terminal_only: bool = False,
) -> Iterator[Finding]:
    """*parent*'s findings, gated as the caller needs.

    The base gate is unconditional and is the rule described in the module
    docstring: a finding needs a type and a report.  The keyword filters
    narrow from there, and each corresponds to one caller's question:

    * ``bugs_only`` — the product is wrong, as opposed to testing being
      obstructed.  Only bugs are counted as defects and only bugs are
      filed.
    * ``ungrouped_only`` — has no ``DefectGroup`` yet.  What makes the
      grouping pass idempotent across a restart, and append-only.
    * ``unfiled_only`` — carries no tracker receipt.  What makes export
      idempotent across a retry.
    * ``terminal_only`` — the case reached a verdict.  Always true on the
      exploratory side.
    """
    for finding in _walk(parent):
        if not finding.finding_type or not finding.title:
            continue
        if bugs_only and finding.finding_type != FindingType.BUG:
            continue
        if ungrouped_only and finding.defect_group_id is not None:
            continue
        if unfiled_only and finding.tracker_issue_key:
            continue
        if terminal_only and not finding.terminal:
            continue
        yield finding


def sprint_for(parent: object) -> Sprint | None:
    """The sprint whose defects *parent*'s findings join.

    A ``TestExecution`` is reached through its **run**, never through its
    requirement: a superseded execution may have an archived or deleted
    requirement, and that is precisely a path where there are still real
    findings to group and file.
    """
    if isinstance(parent, TestExecution):
        run = parent.test_run
        return run.sprint if run is not None else None
    if isinstance(parent, ExploratoryRun):
        return parent.sprint
    return None
