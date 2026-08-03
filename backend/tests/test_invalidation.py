"""Tests for backend/services/invalidation.py — the cascade rules.

Exercised as plain functions against the SQLite fixture, like the
reconciler. The route-level wiring is covered in test_requirement_routes.py;
what matters here is that each rule fires on exactly the right things, and
in particular that the *asymmetries* hold — editing re-opens the
environment, deleting does not, and neither touches a sibling's plan.
"""

import pytest
from sqlmodel import select

from backend.models.database import (
    Requirement,
    TestCase,
    TestEnvironmentStatus,
    TestExecutionStatus,
    TestPlan,
    TestPlanStatus,
)
from backend.services import invalidation
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import (
    _seed_test_case,
    _seed_test_env,
    _seed_test_execution,
    _seed_test_plan,
    _seed_test_run,
)


def _seed_planned(db_session, sprint, name="Login"):
    """A confirmed requirement with an approved plan and one case."""
    requirement = _seed_requirement(db_session, sprint, name=name)
    plan = _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED)
    _seed_test_case(db_session, plan, position=0, title=f"{name} case")
    db_session.refresh(requirement)
    return requirement


class TestRemoveTestPlan:
    def test_deletes_the_plan_but_keeps_its_cases(self, db_session):
        sprint = _seed_sprint(db_session)
        requirement = _seed_planned(db_session, sprint)
        plan_id = requirement.test_plan.id
        case_id = requirement.test_plan.cases[0].id

        invalidation.remove_test_plan(db_session, requirement.test_plan)
        db_session.commit()

        assert db_session.get(TestPlan, plan_id) is None
        case = db_session.exec(select(TestCase).where(TestCase.id == case_id)).one()
        assert case.archived is True
        # Detached, so the unique requirement_id slot is free for the next plan
        assert case.test_plan_id is None

    def test_is_a_noop_on_none(self, db_session):
        invalidation.remove_test_plan(db_session, None)  # must not raise

    def test_frees_the_slot_for_a_regenerated_plan(self, db_session):
        """TestPlan.requirement_id is unique — the row has to go, not archive."""
        sprint = _seed_sprint(db_session)
        requirement = _seed_planned(db_session, sprint)

        invalidation.remove_test_plan(db_session, requirement.test_plan)
        db_session.commit()
        db_session.refresh(requirement)
        assert requirement.test_plan is None

        _seed_test_plan(db_session, requirement)  # must not violate the constraint
        db_session.refresh(requirement)
        assert requirement.test_plan is not None


class TestRequirementChange:
    def test_removes_its_plan_and_reopens_the_environment(self, db_session):
        sprint = _seed_sprint(db_session)
        test_env = _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)
        requirement = _seed_planned(db_session, sprint)
        plan_id = requirement.test_plan.id
        before = requirement.content_revision

        invalidation.invalidate_for_requirement_change(db_session, requirement)
        db_session.commit()

        assert requirement.content_revision == before + 1
        assert db_session.get(TestPlan, plan_id) is None
        assert test_env.status == TestEnvironmentStatus.READY

    def test_leaves_sibling_plans_alone(self, db_session):
        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)
        edited = _seed_planned(db_session, sprint, name="Login")
        sibling = _seed_planned(db_session, sprint, name="Search")
        sibling_plan_id = sibling.test_plan.id

        invalidation.invalidate_for_requirement_change(db_session, edited)
        db_session.commit()

        assert db_session.get(TestPlan, sibling_plan_id) is not None
        assert sibling.content_revision == 0

    def test_does_not_bump_the_environment_revision(self, db_session):
        """Un-confirming is not a content change — blaming the environment
        would attribute the staleness to the wrong artifact."""
        sprint = _seed_sprint(db_session)
        test_env = _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)
        requirement = _seed_planned(db_session, sprint)

        invalidation.invalidate_for_requirement_change(db_session, requirement)
        db_session.commit()

        assert test_env.content_revision == 0


class TestRequirementAdd:
    def test_reopens_the_environment(self, db_session):
        sprint = _seed_sprint(db_session)
        test_env = _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)

        invalidation.invalidate_for_requirement_add(db_session, sprint)
        db_session.commit()

        assert test_env.status == TestEnvironmentStatus.READY

    def test_leaves_existing_plans_alone(self, db_session):
        """Existing plans describe their own, unchanged requirements."""
        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)
        existing = _seed_planned(db_session, sprint)
        plan_id = existing.test_plan.id

        invalidation.invalidate_for_requirement_add(db_session, sprint)
        db_session.commit()

        assert db_session.get(TestPlan, plan_id) is not None

    @pytest.mark.parametrize("status", ["needs_info", "ready"])
    def test_unconfirmed_environment_is_untouched(self, db_session, status):
        sprint = _seed_sprint(db_session)
        test_env = _seed_test_env(db_session, sprint, status=status)

        invalidation.invalidate_for_requirement_add(db_session, sprint)
        db_session.commit()

        assert test_env.status == status


class TestRequirementDelete:
    def test_hard_deletes_when_no_runs_reference_it(self, db_session):
        sprint = _seed_sprint(db_session)
        requirement = _seed_planned(db_session, sprint)
        requirement_id = requirement.id

        invalidation.invalidate_for_requirement_delete(db_session, requirement)
        db_session.commit()

        remaining = db_session.exec(
            select(Requirement.id).where(Requirement.id == requirement_id)
        ).all()
        assert remaining == []

    def test_archives_when_a_run_references_it(self, db_session):
        sprint = _seed_sprint(db_session)
        requirement = _seed_planned(db_session, sprint)
        run = _seed_test_run(db_session, sprint)
        _seed_test_execution(db_session, run, requirement, status=TestExecutionStatus.COMPLETED)
        db_session.refresh(requirement)

        invalidation.invalidate_for_requirement_delete(db_session, requirement)
        db_session.commit()

        assert requirement.archived is True
        assert db_session.get(Requirement, requirement.id) is not None

    def test_leaves_the_environment_confirmed(self, db_session):
        """Removal can only shrink what needs access — the same argument
        TestEnvironmentAccess.requirements_stale already makes."""
        sprint = _seed_sprint(db_session)
        test_env = _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)
        requirement = _seed_planned(db_session, sprint)

        invalidation.invalidate_for_requirement_delete(db_session, requirement)
        db_session.commit()

        assert test_env.status == TestEnvironmentStatus.CONFIRMED

    def test_removes_its_own_plan_only(self, db_session):
        sprint = _seed_sprint(db_session)
        doomed = _seed_planned(db_session, sprint, name="Login")
        sibling = _seed_planned(db_session, sprint, name="Search")
        doomed_plan_id = doomed.test_plan.id
        sibling_plan_id = sibling.test_plan.id

        invalidation.invalidate_for_requirement_delete(db_session, doomed)
        db_session.commit()

        assert db_session.get(TestPlan, doomed_plan_id) is None
        assert db_session.get(TestPlan, sibling_plan_id) is not None


class TestHardDeleteLeavesNoOrphans:
    def test_cases_are_deleted_not_archived_when_no_run_references_them(self, db_session):
        """Archiving only protects rows a run points at.

        On the hard-delete branch there is no run by definition, so archived
        cases would be detached from every parent and reachable from nothing.
        """
        sprint = _seed_sprint(db_session)
        requirement = _seed_planned(db_session, sprint)
        case_id = requirement.test_plan.cases[0].id

        invalidation.invalidate_for_requirement_delete(db_session, requirement)
        db_session.commit()

        assert db_session.exec(select(TestCase.id).where(TestCase.id == case_id)).all() == []

    def test_cases_survive_when_a_run_references_the_requirement(self, db_session):
        sprint = _seed_sprint(db_session)
        requirement = _seed_planned(db_session, sprint)
        case_id = requirement.test_plan.cases[0].id
        run = _seed_test_run(db_session, sprint)
        _seed_test_execution(db_session, run, requirement, status=TestExecutionStatus.COMPLETED)
        db_session.refresh(requirement)

        invalidation.invalidate_for_requirement_delete(db_session, requirement)
        db_session.commit()

        case = db_session.exec(select(TestCase).where(TestCase.id == case_id)).one()
        assert case.archived is True
        assert case.test_plan_id is None


class TestPlanRevisionRestartInvariant:
    """`TestPlan.content_revision` restarts at 0 on a regenerated plan.

    That is only safe because every path which removes a plan also bumps
    something else the run compares — otherwise a run could compare equal
    against a completely different plan and read as current. Nothing in the
    code enforces it, so it is pinned here.
    """

    def test_regenerated_plan_still_leaves_the_old_run_outdated(self, db_session):
        sprint = _seed_sprint(db_session)
        test_env = _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)
        requirement = _seed_planned(db_session, sprint)
        requirement.test_plan.content_revision = 1
        db_session.add(requirement.test_plan)
        db_session.commit()

        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(
            db_session,
            run,
            requirement,
            status=TestExecutionStatus.COMPLETED,
            requirement_revision=requirement.content_revision,
            plan_revision=1,
            env_revision=test_env.content_revision,
        )
        assert execution.outdated_reasons == []

        # Edit the requirement: its plan goes and a fresh one is generated,
        # landing back on content_revision 1 — equal to what the run recorded.
        invalidation.invalidate_for_requirement_change(db_session, requirement)
        db_session.commit()
        new_plan = _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED)
        new_plan.content_revision = 1
        db_session.add(new_plan)
        db_session.commit()
        db_session.expire_all()

        # plan_revision now collides, so the requirement bump is the only
        # thing keeping this honest.
        assert execution.outdated_reasons == ["requirement"]
