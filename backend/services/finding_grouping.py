"""Assign a finished run's bug findings to the sprint's distinct defects.

The sprint's memory of what it has already found lives in ``DefectGroup``
rows, and this module is their only writer.  One pass per completed run,
immediately before ``finding_export`` — which files one ticket per group,
so the grouping has to be committed first.

Three properties shape everything below.

* **Append-only.**  A finding joins an existing group or opens a new one;
  no group and no existing membership is ever rewritten.  That is what
  makes the numbers stable between polls: the non-deterministic judgement
  is resolved once, at completion, and frozen in a row.

* **Independent of any issue tracker.**  A defect is a property of the
  product; a ticket is a property of where you happen to be filing.  This
  pass runs whether or not a tracker is connected, which is the whole
  point — the panel's bug count used to need one.

* **Never raises.**  It is called from a worker task after the run has
  already been marked ``completed``, so a grouping failure must cost the
  grouping (rows keep ``defect_group_id`` null, and ``qa_metrics`` falls
  back to text identity) and never the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import Session, select

from backend.models.database import (
    DefectGroup,
    ExploratoryRun,
    FindingType,
    Sprint,
    TestExecution,
)
from backend.services import finding_dedup
from backend.services.llm_prompts import FindingCandidate, KnownDefect

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Row:
    """One ungrouped bug finding, with the row the group id is written to.

    The two carriers store the same fields under different names, so they
    are read into this shape once and everything below is carrier-blind.
    Deliberately not shared with ``finding_export._Row``, which
    additionally carries a ``FindingReport``, a source label, and a
    screenshot path — none of which a grouping question needs.
    """

    row: object  # TestCaseExecution | ExploratoryFinding
    candidate: FindingCandidate


# ── Reading findings off either carrier ───────────────────────────────


def _rows_for(parent: object) -> list[_Row]:
    """*parent*'s bug findings that have no defect group yet.

    Filters to ``BUG`` here rather than trusting the caller: the grouping
    machinery below is type-blind by design (``FindingCandidate`` carries
    no finding type), and an issue merged into a bug group would surface
    only as a bug count one too low.

    Skipping rows that already carry a group is what makes the pass
    idempotent across a restart, and append-only by construction.
    """
    if isinstance(parent, TestExecution):
        rows = []
        for case in parent.cases:
            # `finding_title` gates this exactly as it gates the run
            # page's own bug list and `qa_metrics` — counting by
            # `finding_type` alone here would group a finding the rest of
            # the application does not show.
            if case.finding_type != FindingType.BUG or not case.finding_title:
                continue
            if case.defect_group_id is not None:
                continue
            rows.append(
                _Row(
                    row=case,
                    candidate=FindingCandidate(
                        severity=case.finding_severity or "",
                        title=case.finding_title,
                        steps_to_reproduce=case.finding_steps_to_reproduce or "",
                        expected=case.finding_expected or "",
                        actual=case.finding_actual or "",
                    ),
                )
            )
        return rows
    if isinstance(parent, ExploratoryRun):
        rows = []
        for exploratory_session in parent.sessions:
            for finding in exploratory_session.findings:
                if finding.finding_type != FindingType.BUG:
                    continue
                if finding.defect_group_id is not None:
                    continue
                rows.append(
                    _Row(
                        row=finding,
                        candidate=FindingCandidate(
                            severity=finding.severity,
                            title=finding.title,
                            steps_to_reproduce=finding.steps_to_reproduce,
                            expected=finding.expected,
                            actual=finding.actual,
                        ),
                    )
                )
        return rows
    logger.warning("Cannot group findings for %r — unknown parent type", type(parent))
    return []


def _sprint_for(parent: object) -> Sprint | None:
    """The sprint whose defects *parent*'s findings join.

    A ``TestExecution`` is reached through its **run**, never through its
    requirement — the same reasoning ``finding_export._spec_for``
    documents: a superseded execution may have an archived or deleted
    requirement, and that is precisely a path that still has real findings.
    """
    if isinstance(parent, TestExecution):
        run = parent.test_run
        return run.sprint if run is not None else None
    if isinstance(parent, ExploratoryRun):
        return parent.sprint
    return None


# ── The one public function ───────────────────────────────────────────


def assign_defect_groups(session: Session, parent: object) -> None:
    """Assign every ungrouped bug finding on *parent* to a defect group.

    Never raises — see the module docstring.  On failure the transaction
    is rolled back rather than left half-applied, because the caller runs
    ``export_findings`` on this same session immediately afterwards and a
    poisoned transaction there would cost the tickets too.
    """
    try:
        _assign(session, parent)
    except Exception:
        logger.exception("Assigning defect groups failed for %r", parent)
        try:
            session.rollback()
        except Exception:
            logger.exception("Rolling back after a failed grouping pass also failed")


def _assign(session: Session, parent: object) -> None:
    rows = _rows_for(parent)
    if not rows:
        # Before the sprint-wide read below, not merely inside
        # `group_findings`' own empty-input guard: by the time that fired,
        # the query building its argument would already have run. A clean
        # run must pay nothing at all. Same shape as `_export`'s fast exit
        # ahead of any config load or network call.
        return

    sprint = _sprint_for(parent)
    if sprint is None:
        logger.warning("Cannot group findings: no sprint reachable from %r", parent)
        return

    # Serialize concurrent passes on this sprint. The deployment runs
    # several workers and one TestRun fans out into a job per
    # TestExecution, so siblings routinely finish together — and what they
    # collide on is the list read just below: each would be shown a list
    # missing the other's groups, and both would create one for the same
    # defect. A unique constraint cannot substitute, because the colliding
    # findings are paraphrases rather than identical text (each comes from
    # its own per-case diagnosis call), and deciding that two paraphrases
    # are one defect *is* the LLM call.
    #
    # Taken *before* the read, because reading a stale list is the bug —
    # by the time the groups are written the wrong decision has been made.
    # Held for one completion bounded by OPENAI_TIMEOUT, contended only by
    # siblings of one run, and released with the transaction if a worker
    # dies. SQLAlchemy renders FOR UPDATE on PostgreSQL and omits it on
    # SQLite, so the test engine needs no branch here.
    session.exec(select(Sprint).where(Sprint.id == sprint.id).with_for_update()).first()

    known = session.exec(
        select(DefectGroup)
        .where(DefectGroup.sprint_id == sprint.id)
        # Newest first: `_prefilter` builds its lookup with `setdefault`,
        # so the first entry for a repeated normalized text wins, and the
        # most recently created group is the right tie-break.
        .order_by(DefectGroup.created_at.desc(), DefectGroup.id.desc())
    ).all()

    candidates = [entry.candidate for entry in rows]
    groups = finding_dedup.group_findings(
        candidates,
        [
            KnownDefect(
                key=str(group.id), title=group.title, expected=group.expected, actual=group.actual
            )
            for group in known
        ],
    )

    by_key = {str(group.id): group for group in known}
    created = joined = 0
    for group in groups:
        target = by_key.get(group.existing_key or "")
        if target is None:
            # A key naming no known group (the model inventing one) lands
            # here too, and opening a new group is the right answer:
            # writing it through would be a dangling foreign key.
            representative = candidates[group.representative]
            target = DefectGroup(
                sprint_id=sprint.id,
                title=representative.title,
                expected=representative.expected,
                actual=representative.actual,
            )
            session.add(target)
            session.flush()  # the id has to exist before the FKs are written
            created += 1
        else:
            joined += 1
        for index in group.members:
            rows[index].row.defect_group_id = target.id
            session.add(rows[index].row)

    session.commit()
    logger.info(
        "Grouped %d finding(s) for %r: %d new defect(s), %d joining a known one",
        len(rows),
        parent,
        created,
        joined,
    )
