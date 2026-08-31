"""Finalize the child rows a run never reached.

Both run types have exactly one writer for their child rows: the loop
inside ``tasks/execute_test.py`` walks ``TestCaseExecution``, the loop
inside ``tasks/explore_requirement.py`` walks ``ExploratorySession``.
Nothing else touches those tables — the reconciler and ``finish_sprint``
sweeps are parametrized over the *parent* row types (``SWEEP_SPECS``).

That is fine while a job runs to the end, and wrong every other way it can
stop.  A superseded execution, a finished sprint, a plan un-approved
underneath the job, an exhausted retry counter, a worker killed mid-case —
each of those finalizes the parent and returns, leaving children `pending`
forever, or `running` forever when the worker died after the row was
stamped.  The user sees a run that says `failed` above a list of cases
still labelled "Queued", one of them spinning.  There is no self-healing
path either: ``restart_test_execution`` refuses an outdated execution, so
the exact case that produces this most often is also the one that can
never be re-run.

So: **a terminal parent has no non-terminal children**, enforced here
rather than at each of the ~8 exits that can reach a terminal parent.

Like ``services/invalidation.py`` (its sibling in spirit — one module owns
a rule several callers need), nothing here commits: it stages on the
caller's session so the parent's failure and its children's disposition
land in one transaction.  It imports only from ``models/database.py``, so
``tasks/`` may use it without breaking the circular-import rule.

The pending/running distinction is kept in ``error`` rather than in a
second status value, because it changes what the reader should *do*: a
`pending` child provably never touched the test environment, while a
`running` one may have executed a script or driven a browser against it
before the worker died.  Same label, different warning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session, update

from backend.config import MAX_AUTO_RETRIES
from backend.models.database import (
    CicdExport,
    CicdExportStatus,
    ExploratoryRun,
    ExploratoryRunStatus,
    ExploratorySession,
    ExploratorySessionStatus,
    NonfunctionalChildStatus,
    NonfunctionalLoadProfile,
    NonfunctionalRun,
    NonfunctionalRunStatus,
    NonfunctionalTarget,
    Requirement,
    RequirementStatus,
    TestCaseExecution,
    TestCaseExecutionStatus,
    TestExecution,
    TestExecutionStatus,
    TestPlan,
    TestPlanStatus,
)

logger = logging.getLogger(__name__)

# Cap on the error text stored on a failed row — long enough to name the
# cause, short enough that a stack trace cannot fill a column.
ERROR_SUMMARY_MAX_CHARS = 300


def now() -> datetime:
    """UTC now, as every status transition in this application stamps it."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ChildSpec:
    """One parent→child pairing this module can finalize.

    Mirrors ``reconciler.SweepSpec``: the status columns share names
    across both models, so only the model, its foreign key back to the
    parent, and the three status values differ.
    """

    model: type
    label: str
    parent_fk: str
    pending_status: str
    running_status: str
    skipped_status: str


TEST_CASE_SPEC = ChildSpec(
    model=TestCaseExecution,
    label="test case",
    parent_fk="test_execution_id",
    pending_status=TestCaseExecutionStatus.PENDING,
    running_status=TestCaseExecutionStatus.RUNNING,
    skipped_status=TestCaseExecutionStatus.SKIPPED,
)

EXPLORATORY_SESSION_SPEC = ChildSpec(
    model=ExploratorySession,
    label="charter session",
    parent_fk="exploratory_run_id",
    pending_status=ExploratorySessionStatus.PENDING,
    running_status=ExploratorySessionStatus.RUNNING,
    skipped_status=ExploratorySessionStatus.SKIPPED,
)


NONFUNCTIONAL_TARGET_SPEC = ChildSpec(
    model=NonfunctionalTarget,
    label="examined URL",
    parent_fk="nonfunctional_run_id",
    pending_status=NonfunctionalChildStatus.PENDING,
    running_status=NonfunctionalChildStatus.RUNNING,
    skipped_status=NonfunctionalChildStatus.SKIPPED,
)

# `skipped` here does **not** mean "safe to re-run", unlike every other
# child spec in this module. A target re-examined costs a page load; a load
# profile re-sent costs real traffic on somebody's environment, and for a
# non-safe method it costs duplicated writes. The never-re-send invariant is
# carried by `NonfunctionalLoadProfile.requests_sent > 0`, which the task
# checks before every profile — not by this status.
LOAD_PROFILE_SPEC = ChildSpec(
    model=NonfunctionalLoadProfile,
    label="load profile",
    parent_fk="nonfunctional_run_id",
    pending_status=NonfunctionalChildStatus.PENDING,
    running_status=NonfunctionalChildStatus.RUNNING,
    skipped_status=NonfunctionalChildStatus.SKIPPED,
)


# ── The parent rows a task drives ─────────────────────────────────────


@dataclass(frozen=True)
class RowSpec:
    """One job-backed parent row type, as its task needs to fail it.

    The machinery columns (``status`` / ``retry_count`` / ``last_heartbeat``
    / ``error`` / ``updated_at``) share names across all four models, so the
    retry protocol below is written once and only these fields differ.

    Deliberately *not* carrying ``clear_field``: the reconciler clears the
    pending-user-input column when it fails a row, and the tasks never have
    — ``generate_test_plan`` documents preserving ``pending_feedback`` so a
    Restart resumes an interrupted revision.  Adding it here would change
    task behaviour under cover of a refactor.  See ``reconciler.SweepSpec``,
    which carries it for the sweeps that do want it.
    """

    model: type
    label: str
    pending_status: str
    failed_status: str
    # Child row types to settle whenever this row reaches `failed_status`.
    # Empty for Requirement/TestPlan, which have no children; a tuple
    # rather than a single spec because a run may drive more than one kind
    # of child row (a nonfunctional run walks targets *and* load profiles).
    child_specs: tuple[ChildSpec, ...] = ()


REQUIREMENT_SPEC = RowSpec(
    model=Requirement,
    label="Requirement",
    pending_status=RequirementStatus.PENDING,
    failed_status=RequirementStatus.FAILED,
)

TEST_PLAN_SPEC = RowSpec(
    model=TestPlan,
    label="Test plan",
    pending_status=TestPlanStatus.PENDING,
    failed_status=TestPlanStatus.FAILED,
)

TEST_EXECUTION_SPEC = RowSpec(
    model=TestExecution,
    label="Test execution",
    pending_status=TestExecutionStatus.PENDING,
    failed_status=TestExecutionStatus.FAILED,
    child_specs=(TEST_CASE_SPEC,),
)

EXPLORATORY_RUN_SPEC = RowSpec(
    model=ExploratoryRun,
    label="Exploratory run",
    pending_status=ExploratoryRunStatus.PENDING,
    failed_status=ExploratoryRunStatus.FAILED,
    child_specs=(EXPLORATORY_SESSION_SPEC,),
)

# An empty `child_specs` is correct rather than an omission: a CicdExport's
# items are receipts written only after the commit succeeds, not work
# units walked by a loop. A failed export has no children to strand.
CICD_EXPORT_SPEC = RowSpec(
    model=CicdExport,
    label="CI/CD export",
    pending_status=CicdExportStatus.PENDING,
    failed_status=CicdExportStatus.FAILED,
    child_specs=(),
)


# The first row type with two kinds of child, which is what `child_specs`
# was widened for: a run walks the URLs it examined and the load profiles it
# applied, and both must be settled when it fails.
NONFUNCTIONAL_RUN_SPEC = RowSpec(
    model=NonfunctionalRun,
    label="Nonfunctional run",
    pending_status=NonfunctionalRunStatus.PENDING,
    failed_status=NonfunctionalRunStatus.FAILED,
    child_specs=(NONFUNCTIONAL_TARGET_SPEC, LOAD_PROFILE_SPEC),
)


def record_failure(session: Session, spec: RowSpec, row_id: int, exc: Exception) -> None:
    """Count a task failure and either re-queue the row or fail it.

    The retry protocol every task shares: roll back whatever the raising
    attempt staged, re-read the row on a clean session, and spend one
    retry.  Under the cap the row goes back to ``pending`` for the
    reconciler to re-enqueue; at the cap it is failed and — for the two
    row types that have children — its unreached children are settled.

    The child cleanup is on the failing branch **only**.  A row going back
    to ``pending`` is resumed exactly where it stopped, so a ``running``
    child there is what the next attempt picks up.

    Commits, unlike the rest of this module: it owns the whole transaction
    because its first act is to discard the caller's.
    """
    session.rollback()
    row = session.get(spec.model, row_id)
    if row is None:
        return

    row.retry_count += 1
    if row.retry_count >= MAX_AUTO_RETRIES:
        row.status = spec.failed_status
        row.error = str(exc)[:ERROR_SUMMARY_MAX_CHARS]
        for child_spec in spec.child_specs:
            abandon_unreached_children(session, child_spec, row_id, row.error)
    else:
        # Back to pending — the reconciler re-enqueues it.
        row.status = spec.pending_status
    row.last_heartbeat = None
    row.updated_at = now()
    session.add(row)
    session.commit()


def fail_row(session: Session, spec: RowSpec, row, error: str) -> None:
    """Fail a row outright and settle every child it never reached.

    Unlike :func:`record_failure` this spends no retry: it is the exit for
    conditions a retry cannot fix — a job-start guard that refused, or an
    upstream edit that superseded the work mid-run.  The single chokepoint
    for those, which is why the child cleanup belongs here rather than at
    each call site.
    """
    row.status = spec.failed_status
    row.error = error
    row.last_heartbeat = None
    row.updated_at = now()
    session.add(row)
    for child_spec in spec.child_specs:
        abandon_unreached_children(session, child_spec, row.id, error)
    session.commit()


def _not_run(reason: str) -> str:
    return f"Not run. {reason}"


def _interrupted(reason: str) -> str:
    return (
        f"Interrupted before it finished, and not resumed. {reason} "
        "It may have partially run against the test environment."
    )


def abandon_unreached_children(
    session: Session, spec: ChildSpec, parent_id: int | None, reason: str
) -> None:
    """Mark every non-terminal child of a finished parent as ``skipped``.

    Two set-based statements rather than one (Convention #9 either way):
    the split by prior status is what lets each row say whether it was
    never started or cut off part-way.

    ``parent_id`` is typed optional only because SQLModel primary keys are
    — a caller always has a persisted row.  Terminal children (passed /
    failed / error / completed, and rows already skipped) are untouched by
    the ``WHERE``, so this is idempotent and safe to call on a parent whose
    loop finished normally.
    """
    if parent_id is None:  # pragma: no cover - defensive, PK is always set here
        return

    now = datetime.now(timezone.utc)
    parent_column = getattr(spec.model, spec.parent_fk)
    for prior_status, error in (
        (spec.pending_status, _not_run(reason)),
        (spec.running_status, _interrupted(reason)),
    ):
        session.exec(
            update(spec.model)
            .where(parent_column == parent_id, spec.model.status == prior_status)
            .values(status=spec.skipped_status, error=error, updated_at=now)
        )

    logger.info("Unreached %s rows for parent id=%s marked skipped", spec.label, parent_id)
