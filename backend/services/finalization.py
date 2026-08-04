"""Finalize the child rows a run never reached.

Both run types have exactly one writer for their child rows: the loop
inside ``tasks/execute_test.py`` walks ``TestCaseExecution``, the loop
inside ``tasks/explore_requirement.py`` walks ``ExploratorySession``.
Nothing else touches those tables — the reconciler and ``finish_sprint``
sweeps are parametrized over the *parent* row types (``_SWEEP_SPECS``).

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

from backend.models.database import (
    ExploratorySession,
    ExploratorySessionStatus,
    TestCaseExecution,
    TestCaseExecutionStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChildSpec:
    """One parent→child pairing this module can finalize.

    Mirrors ``reconciler._SweepSpec``: the status columns share names
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
