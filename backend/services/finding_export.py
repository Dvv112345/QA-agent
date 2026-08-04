"""File a finished run's bug findings into the sprint's issue tracker.

The orchestration layer between three modules that each know one thing:
``finding_dedup`` decides what is a duplicate of what, ``issue_tracker``
speaks HTTP, and this module decides *which* findings to file, reads the
context they need, and writes the receipts back.

**Never raises.**  It is called immediately after a run has been marked
``completed`` — deliberately outside the ``try`` that feeds
``_record_failure`` — so an exception escaping here would turn a finished
run into a retry that re-executes every test case over a tracker outage.
A tracker problem must cost a ticket, never a run.

Two rules shape everything below:

* **Only a run that finished reports automatically.**  Every abnormal
  ending — superseded by an upstream edit, retries exhausted, sprint
  finished underneath it, worker crash — leaves a finding set that is
  incomplete *and known to be incomplete*, and that is not written
  unasked to a tracker other people read.  Those findings are not
  stranded: they sit on the run page and the Retry button files them on
  request.  This module is therefore called from exactly three places —
  the two tasks' completion paths and that button.

* **A ticket is a receipt for an irreversible action.**  Once a key is
  written it is never cleared, not even when the case later passes: the
  finding may stop reporting itself, but the record of having reported it
  to a system this application does not own may not.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session, select

from backend.models.database import (
    ExploratoryFinding,
    ExploratoryRun,
    ExploratorySession,
    FindingType,
    IssueTrackerConfig,
    Sprint,
    TestCaseExecution,
    TestExecution,
    TestRun,
)
from backend.services import finding_dedup, issue_tracker
from backend.services.issue_tracker import (
    FindingContext,
    FindingReport,
    TrackerConfig,
    TrackerError,
)
from backend.services.llm_prompts import FiledFinding, FindingCandidate
from backend.utils.crypto import decrypt_token

logger = logging.getLogger(__name__)

# Cap on the error text stored on a finding whose filing failed — the
# same treatment the task's own error summaries get.
_TRACKER_ERROR_MAX_CHARS = 300

# Refused at run creation when the toggle is on with nothing connected.
# Lives here rather than in each route so both run types word it the same,
# following SPRINT_FINISHED_ERROR's precedent of keeping a user-facing
# string beside the rule it belongs to.
TRACKER_REQUIRED_ERROR = "Connect an issue tracker before filing findings to it."


@dataclass(frozen=True)
class ExportOutcome:
    """What one export pass actually did."""

    filed: int = 0  # issues created
    linked: int = 0  # findings attached to an existing or grouped issue
    failed: int = 0  # findings left carrying a tracker_error


@dataclass(frozen=True)
class _Row:
    """One finding awaiting a ticket, with the row it must be written back to.

    The two carriers store the same seven fields under different names
    (``TestCaseExecution`` prefixes them ``finding_``), so they are read
    into this shape once and everything below is carrier-agnostic.
    """

    row: object  # TestCaseExecution | ExploratoryFinding
    report: FindingReport
    source_label: str  # test-case title | charter text
    screenshot_path: str | None = None


# ── Reading findings off either carrier ───────────────────────────────


def _scripted_rows(execution: TestExecution) -> list[_Row]:
    """This execution's unfiled bug findings, in case order."""
    rows: list[_Row] = []
    for case in execution.cases:
        if case.finding_type != FindingType.BUG or not case.finding_title:
            continue
        if case.tracker_issue_key:
            continue  # already filed — idempotency across retries
        rows.append(
            _Row(
                row=case,
                report=FindingReport(
                    finding_type=FindingType.BUG.value,
                    severity=case.finding_severity or "",
                    title=case.finding_title,
                    steps_to_reproduce=case.finding_steps_to_reproduce or "",
                    expected=case.finding_expected or "",
                    actual=case.finding_actual or "",
                    environment=case.environment,
                ),
                source_label=case.test_case.title if case.test_case else "",
            )
        )
    return rows


def _exploratory_rows(run: ExploratoryRun) -> list[_Row]:
    """This run's unfiled bug findings, in session then position order."""
    rows: list[_Row] = []
    for exploratory_session in run.sessions:
        for finding in exploratory_session.findings:
            if finding.finding_type != FindingType.BUG or finding.tracker_issue_key:
                continue
            rows.append(
                _Row(
                    row=finding,
                    report=FindingReport(
                        finding_type=FindingType.BUG.value,
                        severity=finding.severity,
                        title=finding.title,
                        steps_to_reproduce=finding.steps_to_reproduce,
                        expected=finding.expected,
                        actual=finding.actual,
                        environment=finding.environment,
                    ),
                    source_label=exploratory_session.charter,
                    screenshot_path=finding.screenshot_path,
                )
            )
    return rows


# ── Resolving the parent ──────────────────────────────────────────────


@dataclass(frozen=True)
class _ParentSpec:
    """How to reach everything an export needs from one parent row.

    Modelled on ``finalization.ChildSpec``: the two run types differ only
    in how they reach the sprint, where the toggle lives, and how their
    findings are addressed, so the differences are collected here and the
    logic below is written once.
    """

    sprint: object
    export_findings: bool
    rows: list[_Row]
    run_label: str
    source_kind: str
    requirement_name: str


def _spec_for(parent: object) -> _ParentSpec | None:
    if isinstance(parent, TestExecution):
        run = parent.test_run
        # Reached through the *run*, never through the requirement: a
        # superseded execution may have an archived or deleted
        # requirement, and that is precisely a path where export still
        # has real findings to file.
        return _ParentSpec(
            sprint=run.sprint if run is not None else None,
            export_findings=bool(run is not None and run.export_findings),
            rows=_scripted_rows(parent),
            run_label=f"Scripted run {run.id}" if run is not None else "Scripted run",
            source_kind="scripted",
            requirement_name=parent.requirement_name,
        )
    if isinstance(parent, ExploratoryRun):
        return _ParentSpec(
            sprint=parent.sprint,
            export_findings=bool(parent.export_findings),
            rows=_exploratory_rows(parent),
            run_label=f"Exploratory run {parent.id}",
            source_kind="exploratory",
            requirement_name=parent.requirement_name,
        )
    logger.warning("Cannot export findings for %r — unknown parent type", type(parent))
    return None


# ── Config and context ────────────────────────────────────────────────


def _tracker_config(config: IssueTrackerConfig) -> TrackerConfig | None:
    """The row as the transport wants it, with the token decrypted."""
    try:
        token = decrypt_token(config.api_token)
    except Exception:
        # Never the token or the ciphertext in the log.
        logger.warning("Sprint id=%s: tracker token could not be decrypted", config.sprint_id)
        return None
    return TrackerConfig(
        provider=config.provider,
        target=config.target,
        api_token=token,
        base_url=config.base_url,
        account_email=config.account_email,
        issue_type=config.issue_type,
    )


def _secret_values(sprint: Sprint) -> frozenset[str]:
    """Environment values to blank out of ticket text, minus the base URLs.

    Same rule the exploratory action log uses: a URL is something a bug
    report has to be allowed to name, and redacting it would gut the
    report while protecting nothing.
    """
    test_env = sprint.test_environment
    env_vars = test_env.env_vars if test_env is not None else None
    if not env_vars:
        return frozenset()
    base_urls = {
        value
        for value in env_vars.values()
        if isinstance(value, str) and value.startswith(("http://", "https://"))
    }
    return frozenset(set(env_vars.values()) - base_urls)


def _already_filed(session: Session, sprint: Sprint, tracker_target: str) -> list[FiledFinding]:
    """Bug findings already filed for this sprint, newest first.

    Filtered on ``tracker_target``, not just the sprint.  The config is
    editable, and without the filter a sprint re-pointed at another
    project would offer keys belonging to the tracker it used to point
    at.  On Jira an adopted stale key merely 404s; on GitHub, whose issue
    numbers are per-repo integers, repo B's ``#7`` answers
    ``issue_is_open`` for repo A's ``#7`` and the finding is attached to
    an unrelated ticket.
    """
    scripted = session.exec(
        select(TestCaseExecution)
        .join(TestExecution, TestCaseExecution.test_execution_id == TestExecution.id)
        .join(TestRun, TestExecution.test_run_id == TestRun.id)
        .where(TestRun.sprint_id == sprint.id)
        .where(TestCaseExecution.tracker_issue_key.is_not(None))
        .where(TestCaseExecution.tracker_target == tracker_target)
    ).all()
    exploratory = session.exec(
        select(ExploratoryFinding)
        .join(
            ExploratorySession,
            ExploratoryFinding.exploratory_session_id == ExploratorySession.id,
        )
        .join(ExploratoryRun, ExploratorySession.exploratory_run_id == ExploratoryRun.id)
        .where(ExploratoryRun.sprint_id == sprint.id)
        .where(ExploratoryFinding.tracker_issue_key.is_not(None))
        .where(ExploratoryFinding.tracker_target == tracker_target)
    ).all()

    # Sorted newest-first, which is the order `finding_dedup` reads to
    # pick the most recent ticket when a defect matches several.
    dated: list[tuple[object, FiledFinding]] = [
        (
            case.updated_at,
            FiledFinding(
                issue_key=case.tracker_issue_key,
                title=case.finding_title or "",
                expected=case.finding_expected or "",
                actual=case.finding_actual or "",
            ),
        )
        for case in scripted
    ] + [
        (
            finding.created_at,
            FiledFinding(
                issue_key=finding.tracker_issue_key,
                title=finding.title,
                expected=finding.expected,
                actual=finding.actual,
            ),
        )
        for finding in exploratory
    ]
    dated.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in dated]


def _also_observed(rows: list[_Row], duplicates: list[int], run_label: str) -> tuple[str, ...]:
    """The other findings this ticket stands for, one line each.

    Title, run, and timestamp rather than the title alone: nothing is
    ever appended to a ticket afterwards, so this list is the ticket's
    entire record of how often and when the defect was seen.
    """
    observed = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return tuple(
        f"{rows[index].report.title} — {run_label}, {observed}" for index in sorted(duplicates)
    )


# ── Persisting ────────────────────────────────────────────────────────


def _write_success(entry: _Row, key: str, url: str, target: str, *, duplicate: bool) -> None:
    entry.row.tracker_issue_key = key
    entry.row.tracker_issue_url = url
    entry.row.tracker_target = target
    entry.row.tracker_is_duplicate = duplicate
    entry.row.tracker_error = None


def _write_failure(entry: _Row, message: str) -> None:
    """Leave the key unset so a retry re-elects and re-files the group."""
    entry.row.tracker_issue_key = None
    entry.row.tracker_issue_url = None
    entry.row.tracker_target = None
    entry.row.tracker_is_duplicate = False
    entry.row.tracker_error = message[:_TRACKER_ERROR_MAX_CHARS]


def _attach_screenshot(config: TrackerConfig, key: str, entry: _Row) -> None:
    """Best-effort image upload for the representative finding only.

    A duplicate files nothing, so it has no ticket of its own to
    illustrate.  Every failure path here is silent: with
    ``STORE_OFFLINE=false`` there is simply no file, which is the
    documented outcome of that setting rather than an error.
    """
    path = entry.screenshot_path
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, "rb") as handle:
            png = handle.read()
    except OSError as exc:
        logger.warning("Could not read screenshot %s: %s", path, exc)
        return
    issue_tracker.attach_screenshot(config, key, png, os.path.basename(path))


# ── The one public function ───────────────────────────────────────────


def export_findings(session: Session, parent: object) -> ExportOutcome:
    """File *parent*'s unfiled bug findings; never raises.

    Commits per group, so a failure part-way keeps every ticket that
    already landed — the alternative is re-filing them on the next
    attempt, which is the one mistake this whole module is arranged to
    avoid.
    """
    try:
        return _export(session, parent)
    except Exception:
        logger.exception("Exporting findings failed for %r", parent)
        return ExportOutcome()


def _export(session: Session, parent: object) -> ExportOutcome:
    spec = _spec_for(parent)
    if spec is None:
        return ExportOutcome()

    # Fast exit before any config load or network call. This is what makes
    # calling export from every completion path free, and it is why every
    # pre-existing run test is unaffected: export_findings defaults false.
    if not spec.export_findings or not spec.rows:
        return ExportOutcome()

    sprint = spec.sprint
    if sprint is None:
        logger.warning("Cannot export findings: no sprint reachable from %r", parent)
        return ExportOutcome()

    tracker_row = sprint.issue_tracker
    if tracker_row is None:
        # The tracker was disconnected between run start and completion.
        # Recorded on the findings so the run page can offer a retry once
        # something is connected again, rather than silently doing nothing.
        for entry in spec.rows:
            _write_failure(entry, "No issue tracker is connected to this sprint.")
            session.add(entry.row)
        session.commit()
        return ExportOutcome(failed=len(spec.rows))

    config = _tracker_config(tracker_row)
    if config is None:
        for entry in spec.rows:
            _write_failure(entry, "The stored issue-tracker token could not be read.")
            session.add(entry.row)
        session.commit()
        return ExportOutcome(failed=len(spec.rows))

    target = tracker_row.tracker_target
    secrets = _secret_values(sprint)
    already_filed = _already_filed(session, sprint, target)
    candidates = [
        FindingCandidate(
            severity=entry.report.severity,
            title=entry.report.title,
            steps_to_reproduce=entry.report.steps_to_reproduce,
            expected=entry.report.expected,
            actual=entry.report.actual,
        )
        for entry in spec.rows
    ]
    groups = finding_dedup.group_findings(candidates, already_filed)

    filed = linked = failed = 0
    for group in groups:
        representative = spec.rows[group.representative]
        members = [spec.rows[index] for index in group.members]

        key: str | None = None
        url: str | None = None
        superseded_key: str | None = None

        if group.existing_key:
            if issue_tracker.issue_is_open(config, group.existing_key):
                # Adopt it. Deliberately no comment on the ticket: the
                # findings are all readable in-app, and appending to
                # somebody's ticket on every re-run is noise.
                key = group.existing_key
                url = _existing_url(session, sprint, target, group.existing_key)
            else:
                # Closed is a decision somebody made. Reopening it
                # silently would undo that, so a new ticket back-refers.
                superseded_key = group.existing_key

        if key is None:
            context = FindingContext(
                sprint_name=sprint.name,
                requirement_name=spec.requirement_name,
                run_label=spec.run_label,
                source_label=representative.source_label,
                source_kind=spec.source_kind,
                secret_values=secrets,
                also_observed=_also_observed(spec.rows, group.duplicates, spec.run_label),
                superseded_key=superseded_key,
            )
            try:
                ref = issue_tracker.create_issue(config, representative.report, context)
            except TrackerError as exc:
                # Every member, not just the representative: a retry has
                # to re-elect and re-file the whole group, which it can
                # only do if none of them carries a key.
                for entry in members:
                    _write_failure(entry, str(exc))
                    session.add(entry.row)
                session.commit()
                failed += len(members)
                logger.warning("Could not file a finding group: %s", exc)
                continue
            key, url = ref.key, ref.url
            filed += 1
            _attach_screenshot(config, key, representative)
        else:
            linked += 1

        _write_success(representative, key, url or "", target, duplicate=False)
        session.add(representative.row)
        for entry in members:
            if entry is representative:
                continue
            _write_success(entry, key, url or "", target, duplicate=True)
            session.add(entry.row)
            linked += 1
        session.commit()

    logger.info(
        "Export for %s: %d filed, %d linked, %d failed", spec.run_label, filed, linked, failed
    )
    return ExportOutcome(filed=filed, linked=linked, failed=failed)


def _existing_url(session: Session, sprint: Sprint, target: str, key: str) -> str:
    """The URL a previous filing already recorded for *key*.

    Read back rather than reconstructed: the URL shape differs per
    provider and per Jira site, and this module has no business knowing
    either.  An empty string is the honest answer when nothing recorded
    it — the key is still what a human quotes.
    """
    for finding in _filed_rows_with_key(session, sprint, target, key):
        if finding.tracker_issue_url:
            return finding.tracker_issue_url
    return ""


def _filed_rows_with_key(session: Session, sprint: Sprint, target: str, key: str):
    """Every persisted finding already carrying *key* for this tracker."""
    cases = session.exec(
        select(TestCaseExecution)
        .where(TestCaseExecution.tracker_issue_key == key)
        .where(TestCaseExecution.tracker_target == target)
    ).all()
    findings = session.exec(
        select(ExploratoryFinding)
        .where(ExploratoryFinding.tracker_issue_key == key)
        .where(ExploratoryFinding.tracker_target == target)
    ).all()
    yield from cases
    yield from findings
