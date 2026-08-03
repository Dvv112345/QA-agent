"""Cascade rules for editing a confirmed artifact.

The pipeline used to be a one-way ratchet: once a requirement was confirmed,
the environment confirmed, and a plan approved, none of them could change.
That is what kept everything downstream consistent for free.  With those
gates opened, something has to decide what an edit invalidates — and it has
to be one thing, because four callers need the identical answer (requirement
edit, add, delete, and the PRD bulk replace, which is an add *and* a delete
at once).

Every function here stages its changes on the caller's session and never
commits, so a route keeps the edit and its cascade in a single transaction:
a failure part-way leaves the sprint untouched rather than half-invalidated.

The rules, and why they are not symmetric:

* **Editing** a requirement removes its plan (written against text that no
  longer exists) and un-confirms the environment (the access description was
  judged sufficient for a requirement set that has changed).
* **Adding** a requirement un-confirms the environment for the same reason,
  but removes no plan — the existing ones are still about their own,
  unchanged requirements.
* **Deleting** a requirement removes only its own plan and leaves the
  environment confirmed.  ``TestEnvironmentAccess.requirements_stale``
  already made this argument: removal can only shrink the environments
  needed, so there is nothing new to check.
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from backend.models.database import (
    ExploratoryRunStatus,
    Requirement,
    Sprint,
    TestEnvironmentStatus,
    TestExecutionStatus,
    TestPlan,
    TestPlanStatus,
)

logger = logging.getLogger(__name__)

# Statuses that mean a worker may be holding this row right now.
_PLAN_IN_FLIGHT = (TestPlanStatus.PENDING, TestPlanStatus.GENERATING)
_EXECUTION_IN_FLIGHT = (TestExecutionStatus.PENDING, TestExecutionStatus.RUNNING)
_EXPLORATION_IN_FLIGHT = (ExploratoryRunStatus.PENDING, ExploratoryRunStatus.RUNNING)


def work_in_flight(requirements: list[Requirement]) -> list[str]:
    """Names of requirements a worker is currently busy with.

    Callers turn a non-empty result into a 422.  This exists because a
    cascade *deletes* a ``TestPlan`` row, and ``execute_test`` would fault
    on the missing row mid-job — its "plan no longer approved" guard covers
    the tidy case but not a vanished one.  Cheaper to make the state
    unreachable than to harden every task against it.
    """
    blocked: list[str] = []
    for requirement in requirements:
        plan = requirement.test_plan
        if (
            (plan is not None and plan.status in _PLAN_IN_FLIGHT)
            or any(e.status in _EXECUTION_IN_FLIGHT for e in requirement.test_executions)
            or any(r.status in _EXPLORATION_IN_FLIGHT for r in requirement.exploratory_runs)
        ):
            blocked.append(requirement.name)
    return blocked


def remove_test_plan(session: Session, plan: TestPlan | None) -> None:
    """Discard a plan while keeping the cases any past run executed.

    The cases are archived and *detached* rather than deleted: a
    ``TestCaseExecution`` reads its title, steps, and expected result off
    those rows, and removing them would rewrite the record of a run that
    already happened.

    The plan row itself does go, because ``TestPlan.requirement_id`` is
    unique — archiving it in place would leave no slot for the regenerated
    plan.  Deleting it also means ``generate_test_plans`` sees ``plan is
    None`` and rebuilds, with no change to its existing logic.
    """
    if plan is None:
        return
    requirement = plan.requirement
    for case in plan.all_cases:
        case.archived = True
        case.test_plan_id = None
        session.add(case)
    session.delete(plan)

    # Flush the delete and drop the parent's stale in-memory reference.
    # Without this, a later ``session.add(requirement)`` — which the delete
    # cascade does when it archives — cascades save-update onto the deleted
    # TestPlan and raises. Flushing is not committing: a rollback still
    # undoes the whole cascade with the edit that triggered it.
    session.flush()
    if requirement is not None:
        session.expire(requirement, ["test_plan"])


def unconfirm_environment(session: Session, sprint: Sprint) -> None:
    """Send a confirmed environment back for re-checking.

    Only the status moves — ``content_revision`` is deliberately untouched,
    because nothing about the access description itself changed.  Marking
    runs environment-outdated here would blame the wrong artifact.

    Dropping out of ``confirmed`` also re-closes the plan-generation gate
    (``Sprint.environment_confirmed``), which is the point: plans must not
    be generated against a requirement set the environment was never
    assessed for.
    """
    test_env = sprint.test_environment
    if test_env is None or test_env.status != TestEnvironmentStatus.CONFIRMED:
        return
    test_env.status = TestEnvironmentStatus.READY
    session.add(test_env)
    logger.info("Sprint id=%s: test environment un-confirmed for re-checking", sprint.id)


def invalidate_for_requirement_change(session: Session, requirement: Requirement) -> None:
    """A confirmed requirement's text changed: drop its plan, re-open the env."""
    requirement.content_revision += 1
    session.add(requirement)
    remove_test_plan(session, requirement.test_plan)
    if requirement.sprint is not None:
        unconfirm_environment(session, requirement.sprint)


def invalidate_for_requirement_add(session: Session, sprint: Sprint) -> None:
    """A requirement joined the set: the environment must be re-assessed.

    Existing plans are left alone — they describe their own requirements,
    which have not changed.
    """
    unconfirm_environment(session, sprint)


def invalidate_for_environment_change(session: Session, sprint: Sprint) -> None:
    """The access description (or its variables) changed: every plan goes.

    Sprint-wide rather than per-requirement because every plan in the sprint
    was generated with the old description in its prompt.

    Callers bump ``content_revision`` themselves rather than having it done
    here: the variables edit must bump it *without* stamping ``updated_at``,
    which on this row means "last LLM check" and is what
    ``requirements_stale`` compares against.
    """
    for requirement in sprint.requirements:
        remove_test_plan(session, requirement.test_plan)


def invalidate_for_requirement_delete(session: Session, requirement: Requirement) -> None:
    """Remove a requirement, preserving any run that already used it.

    Archived rather than deleted **only when there is history to protect**.
    A requirement with no runs behind it is removed outright: archiving it
    would leave the sprint carrying invisible rows forever, worst on the PRD
    re-upload path, which replaces its whole set on every upload.

    The environment stays confirmed — see the module docstring.
    """
    remove_test_plan(session, requirement.test_plan)

    if requirement.test_executions or requirement.exploratory_runs:
        requirement.archived = True
        session.add(requirement)
        logger.info("Requirement id=%s archived (runs reference it)", requirement.id)
    else:
        session.delete(requirement)
        logger.info("Requirement id=%s deleted (no runs reference it)", requirement.id)
