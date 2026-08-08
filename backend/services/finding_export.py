"""File a finished run's bug findings into the sprint's issue tracker.

The orchestration layer between three modules that each know one thing:
``finding_grouping`` decides what is an occurrence of what,
``issue_tracker`` speaks HTTP, and this module decides *which* findings to
file, reads the context they need, and writes the receipts back.

Grouping is **consumed, not computed**.  This module used to run its own
``finding_dedup`` pass, whose answer nobody else could see — so the panel
and the tracker could report different groupings of the same findings.  It
now reads ``defect_group_id`` off the rows, and calls
``assign_defect_groups`` itself first so that filing a run whose pass never
ran still groups before it writes to a system this application does not
own.  What it keeps from ``finding_dedup`` is the election: whose report
becomes the ticket.

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

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from backend.models.database import (
    DefectGroupTicket,
    ExploratoryRun,
    FindingType,
    IssueTrackerConfig,
    Sprint,
    TestExecution,
)
from backend.services import finding_dedup, finding_grouping, issue_tracker
from backend.services.issue_tracker import (
    FindingContext,
    FindingReport,
    TrackerConfig,
    TrackerError,
)
from backend.services.llm_prompts import FindingCandidate
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
        # `finding_type` is derived from `status` alone, so the title check
        # is not redundant with it — and it is the last guard before an
        # outbound write: a case marked `failed` with no report would file
        # an empty ticket into Jira or GitHub, which this application
        # cannot take back.
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


# ── Grouping, and the ticket a group already holds ────────────────────


def _bucket_key(entry: _Row) -> tuple:
    """Which defect this finding is an occurrence of.

    Read off the row **after** ``assign_defect_groups`` has committed, not
    snapshotted into ``_Row`` by ``_spec_for``, which runs before it.

    The same shape ``qa_metrics._defect_key`` uses, minus its ticket
    branch: a receipt cannot be a *grouping* key here, since every row
    that reaches this point is unfiled by definition.  A ``("text", …)``
    bucket therefore means the assignment pass never grouped the row,
    which after the call above only happens if it hit an unexpected
    exception — and text identity is then the best evidence left.
    """
    if entry.row.defect_group_id is not None:
        return ("group", entry.row.defect_group_id)
    return (
        "text",
        finding_dedup.dedup_key(entry.report.title, entry.report.expected, entry.report.actual),
    )


def _ticket_for(
    session: Session, defect_group_id: int | None, target: str
) -> DefectGroupTicket | None:
    """The ticket this defect already holds **in this tracker**, if any.

    One lookup, no fork.  No row for this target means the defect has
    never been filed *here*, so it files fresh — the defect is the same,
    the tracker is not, and the team is now looking at the new one.  The
    target scoping is not optional in the other direction either: GitHub
    issue numbers are per-repo integers, so an unscoped lookup would adopt
    repo B's ``#7`` for repo A's, which is the exact failure
    ``tracker_target`` was introduced for.
    """
    if defect_group_id is None:
        return None
    return session.exec(
        select(DefectGroupTicket)
        .where(DefectGroupTicket.defect_group_id == defect_group_id)
        .where(DefectGroupTicket.tracker_target == target)
    ).first()


def _record_group_ticket(
    session: Session, defect_group_id: int | None, target: str, key: str, url: str
) -> None:
    """Point this defect's row for *target* at the ticket just created.

    An **upsert**, because the unique constraint makes it the only legal
    move: a second insert for the same ``(group, target)`` raises.  The
    update path is the supersession case — the row named a ticket somebody
    closed, a replacement has just been filed carrying a
    ``Previously filed as …`` back-reference, and the row has to name the
    replacement now, because that is what the next run should try to
    adopt.  Nothing is lost by overwriting: the chain lives in the
    tracker, and every finding filed under the closed key keeps its own
    receipt.

    The insert goes inside a **savepoint**.  This write shares the
    per-group ``session.commit()`` with the receipts, and that commit runs
    *after* ``create_issue`` already succeeded — so an ``IntegrityError``
    escaping here would fail the commit, escape ``_export``, and be
    swallowed by ``export_findings``' catch-all, leaving a ticket in the
    tracker that no finding carries the key of.  Two sibling jobs
    completing together make that reachable.  The savepoint keeps the
    failed insert from poisoning the transaction, and the row the other
    job wrote is the correct answer anyway.
    """
    if defect_group_id is None:
        return  # an ungrouped bucket has no defect to record the ticket against
    existing = _ticket_for(session, defect_group_id, target)
    if existing is not None:
        existing.issue_key = key
        existing.issue_url = url
        # Explicit: `default_factory` fires on insert only, so leaving it
        # would keep the superseded ticket's timestamp on a row that now
        # names its replacement.
        existing.filed_at = datetime.now(timezone.utc)
        session.add(existing)
        return
    try:
        with session.begin_nested():
            session.add(
                DefectGroupTicket(
                    defect_group_id=defect_group_id,
                    tracker_target=target,
                    issue_key=key,
                    issue_url=url,
                )
            )
    except IntegrityError:
        won = _ticket_for(session, defect_group_id, target)
        logger.info(
            "Defect group %s already had a ticket for %s (%s); keeping it",
            defect_group_id,
            target,
            won.issue_key if won is not None else "unknown",
        )


def _also_observed(duplicates: list[_Row], run_label: str) -> tuple[str, ...]:
    """The other findings this ticket stands for, one line each.

    Title, run, and timestamp rather than the title alone: nothing is
    ever appended to a ticket afterwards, so this list is the ticket's
    entire record of how often and when the defect was seen.
    """
    observed = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return tuple(f"{entry.report.title} — {run_label}, {observed}" for entry in duplicates)


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


def export_findings(session: Session, parent: object, *, requested: bool = False) -> ExportOutcome:
    """File *parent*'s unfiled bug findings; never raises.

    Commits per group, so a failure part-way keeps every ticket that
    already landed — the alternative is re-filing them on the next
    attempt, which is the one mistake this whole module is arranged to
    avoid.

    *requested* separates the two callers, and the distinction is the
    whole reason the run's toggle exists.  The toggle decides what happens
    **unasked**: the tasks call this on their completion path with
    ``requested=False``, and a run whose toggle was off files nothing.
    The retry routes pass ``requested=True``, because a user pressing
    "File 6 bugs" *is* the consent the toggle stands in for — reading the
    toggle there would leave a run started before the tracker was
    connected with no way to file its bugs at all, behind a button that
    silently did nothing.
    """
    try:
        return _export(session, parent, requested=requested)
    except Exception:
        logger.exception("Exporting findings failed for %r", parent)
        return ExportOutcome()


def _export(session: Session, parent: object, *, requested: bool) -> ExportOutcome:
    spec = _spec_for(parent)
    if spec is None:
        return ExportOutcome()

    # Fast exit before any config load or network call. This is what makes
    # calling export from every completion path free, and it is why every
    # pre-existing run test is unaffected: export_findings defaults false.
    #
    # The `not spec.rows` half stays unconditional — a run with nothing to
    # file is a no-op however it got here, which is what keeps the retry
    # button from being a trap.
    if (not requested and not spec.export_findings) or not spec.rows:
        return ExportOutcome()

    sprint = spec.sprint
    if sprint is None:
        logger.warning("Cannot export findings: no sprint reachable from %r", parent)
        return ExportOutcome()

    # One rule, no special cases: **grouping happens when a run completes,
    # and again before anything is filed.** Filing has always grouped
    # first — this used to call `finding_dedup.group_findings` itself — so
    # this is that responsibility using the shared, persisted answer
    # instead of a private one nobody else could see.
    #
    # It costs nothing in the common case: a retry after a `tracker_error`
    # is on a completed run whose rows are already grouped, so the pass
    # fast-exits with no LLM call and no query. The case it exists for is
    # the other one — a run that never completed, filed by hand, whose
    # paraphrased duplicates would otherwise each become a permanent
    # ticket in a system this application does not own.
    finding_grouping.assign_defect_groups(session, parent)

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

    # One bucket per distinct defect, in row order.
    buckets: dict[tuple, list[_Row]] = {}
    for entry in spec.rows:
        buckets.setdefault(_bucket_key(entry), []).append(entry)

    filed = linked = failed = 0
    for bucket_key, members in buckets.items():
        defect_group_id = bucket_key[1] if bucket_key[0] == "group" else None
        # Whose report becomes the ticket is still elected in code from a
        # *live row* — the group's frozen text is for matching, while a
        # ticket body needs steps, severity, environment, and a screenshot
        # path, which only the row carries.
        candidates = [
            FindingCandidate(
                severity=entry.report.severity,
                title=entry.report.title,
                steps_to_reproduce=entry.report.steps_to_reproduce,
                expected=entry.report.expected,
                actual=entry.report.actual,
            )
            for entry in members
        ]
        elected = finding_dedup.elect_representative(candidates, list(range(len(members))))
        representative = members[elected]
        duplicates = [entry for entry in members if entry is not representative]

        key: str | None = None
        url: str | None = None
        superseded_key: str | None = None

        existing = _ticket_for(session, defect_group_id, target)
        if existing is not None:
            if issue_tracker.issue_is_open(config, existing.issue_key):
                # Adopt it. Deliberately no comment on the ticket: the
                # findings are all readable in-app, and appending to
                # somebody's ticket on every re-run is noise.
                key = existing.issue_key
                url = existing.issue_url
            else:
                # Closed is a decision somebody made. Reopening it
                # silently would undo that, so a new ticket back-refers.
                superseded_key = existing.issue_key

        if key is None:
            context = FindingContext(
                sprint_name=sprint.name,
                requirement_name=spec.requirement_name,
                run_label=spec.run_label,
                source_label=representative.source_label,
                source_kind=spec.source_kind,
                secret_values=secrets,
                also_observed=_also_observed(duplicates, spec.run_label),
                superseded_key=superseded_key,
            )
            try:
                ref = issue_tracker.create_issue(config, representative.report, context)
            except TrackerError as exc:
                # Every member, not just the representative: a retry has
                # to re-elect and re-file the whole group, which it can
                # only do if none of them carries a key. The group's own
                # ticket row is left untouched too, so a supersede that
                # failed re-files rather than adopting a ticket that was
                # never created.
                for entry in members:
                    _write_failure(entry, str(exc))
                    session.add(entry.row)
                session.commit()
                failed += len(members)
                logger.warning("Could not file a finding group: %s", exc)
                continue
            key, url = ref.key, ref.url
            filed += 1
            _record_group_ticket(session, defect_group_id, target, key, url or "")
            _attach_screenshot(config, key, representative)
        else:
            linked += 1

        _write_success(representative, key, url or "", target, duplicate=False)
        session.add(representative.row)
        for entry in duplicates:
            _write_success(entry, key, url or "", target, duplicate=True)
            session.add(entry.row)
            linked += 1
        session.commit()

    logger.info(
        "Export for %s: %d filed, %d linked, %d failed", spec.run_label, filed, linked, failed
    )
    return ExportOutcome(filed=filed, linked=linked, failed=failed)
