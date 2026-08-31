"""Tests for the nonfunctional run's four tables.

Shapes only — the check layer, the browser executors, the task and the
findings walk each arrive in their own phase. The seeding helpers here are
the ones every later nonfunctional suite imports.
"""

from backend.models.database import (
    DefectPool,
    LoadMethod,
    NonfunctionalChildStatus,
    NonfunctionalDomain,
    NonfunctionalFinding,
    NonfunctionalLoadProfile,
    NonfunctionalRun,
    NonfunctionalRunStatus,
    NonfunctionalTarget,
    RequirementStatus,
    TargetKind,
    TestEnvironmentStatus,
    TestPlanStatus,
)
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import _seed_test_env, _seed_test_plan

# ── Seeding helpers (imported by every later nonfunctional suite) ─────


def _seed_nonfunctional_run(db_session, sprint, requirement, **kwargs):
    kwargs.setdefault("base_url_env_vars_csv", "BASE_URL")
    kwargs.setdefault(
        "domains_csv",
        ",".join(
            (
                NonfunctionalDomain.ACCESSIBILITY,
                NonfunctionalDomain.PERFORMANCE,
                NonfunctionalDomain.SECURITY,
            )
        ),
    )
    run = NonfunctionalRun(sprint_id=sprint.id, requirement_id=requirement.id, **kwargs)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _seed_nonfunctional_target(db_session, run, position=0, **kwargs):
    kwargs.setdefault("url", "https://staging.example.com/login")
    target = NonfunctionalTarget(nonfunctional_run_id=run.id, position=position, **kwargs)
    db_session.add(target)
    db_session.commit()
    db_session.refresh(target)
    return target


def _seed_load_profile(db_session, run, position=0, **kwargs):
    kwargs.setdefault("url", "https://staging.example.com/api/items")
    profile = NonfunctionalLoadProfile(nonfunctional_run_id=run.id, position=position, **kwargs)
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)
    return profile


def _seed_nonfunctional_finding(db_session, target, position=0, **kwargs):
    from backend.models.database import FindingSeverity, FindingType

    kwargs.setdefault("domain", NonfunctionalDomain.ACCESSIBILITY)
    kwargs.setdefault("rule", "image-alt")
    kwargs.setdefault("finding_type", FindingType.BUG)
    kwargs.setdefault("severity", FindingSeverity.HIGH)
    kwargs.setdefault("title", "Images have no alternative text")
    kwargs.setdefault("steps_to_reproduce", "Open the login page\nInspect the logo image")
    kwargs.setdefault("expected", "Every image carries alt text")
    kwargs.setdefault("actual", "2 images have no alt attribute")
    finding = NonfunctionalFinding(nonfunctional_target_id=target.id, position=position, **kwargs)
    db_session.add(finding)
    db_session.commit()
    db_session.refresh(finding)
    return finding


def _sprint_with_requirement(db_session, **run_kwargs):
    sprint = _seed_sprint(db_session)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED)
    _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)
    db_session.refresh(sprint)
    run = _seed_nonfunctional_run(db_session, sprint, requirement, **run_kwargs)
    return sprint, requirement, run


# ── Round trips ───────────────────────────────────────────────────────


class TestNonfunctionalRowsRoundTrip:
    def test_run_target_profile_and_finding_persist(self, db_session):
        _sprint, _requirement, run = _sprint_with_requirement(db_session)
        target = _seed_nonfunctional_target(db_session, run, kind=TargetKind.ENDPOINT)
        profile = _seed_load_profile(
            db_session, run, method=LoadMethod.POST, body='{"q": "$TOKEN"}'
        )
        finding = _seed_nonfunctional_finding(db_session, target)

        db_session.expire_all()
        stored = db_session.get(NonfunctionalRun, run.id)
        assert stored.status == NonfunctionalRunStatus.PENDING
        assert stored.targets == [db_session.get(NonfunctionalTarget, target.id)]
        assert stored.load_profiles == [db_session.get(NonfunctionalLoadProfile, profile.id)]
        assert stored.targets[0].findings[0].id == finding.id
        assert stored.targets[0].kind == TargetKind.ENDPOINT
        # The body is stored with its placeholder — resolution happens
        # inside the load runner and no resolved value is ever persisted.
        assert stored.load_profiles[0].body == '{"q": "$TOKEN"}'

    def test_csv_columns_decode_through_their_properties(self, db_session):
        _sprint, _requirement, run = _sprint_with_requirement(
            db_session,
            base_url_env_vars_csv="BASE_URL,API_URL",
            domains_csv="accessibility,security",
        )

        assert run.base_url_env_vars == ["BASE_URL", "API_URL"]
        assert run.domains == ["accessibility", "security"]

    def test_child_statuses_and_counters_default(self, db_session):
        _sprint, _requirement, run = _sprint_with_requirement(db_session)
        target = _seed_nonfunctional_target(db_session, run)
        profile = _seed_load_profile(db_session, run)

        assert target.status == NonfunctionalChildStatus.PENDING
        assert target.kind == TargetKind.PAGE
        assert (target.a11y_outcome, target.security_outcome, target.performance_outcome) == (
            None,
            None,
            None,
        )
        assert profile.status == NonfunctionalChildStatus.PENDING
        # The never-re-send invariant reads this column and nothing else.
        assert profile.requests_sent == 0

    def test_deleting_a_run_takes_its_children_with_it(self, db_session):
        _sprint, _requirement, run = _sprint_with_requirement(db_session)
        target = _seed_nonfunctional_target(db_session, run)
        profile = _seed_load_profile(db_session, run)
        finding = _seed_nonfunctional_finding(db_session, target)

        db_session.delete(run)
        db_session.commit()

        assert db_session.get(NonfunctionalTarget, target.id) is None
        assert db_session.get(NonfunctionalLoadProfile, profile.id) is None
        assert db_session.get(NonfunctionalFinding, finding.id) is None

    def test_has_screenshot_reports_the_path_without_exposing_it(self, db_session):
        _sprint, _requirement, run = _sprint_with_requirement(db_session)
        target = _seed_nonfunctional_target(db_session, run)

        without = _seed_nonfunctional_finding(db_session, target, position=0)
        with_shot = _seed_nonfunctional_finding(
            db_session, target, position=1, screenshot_path="/tmp/shot.png"
        )

        assert without.has_screenshot is False
        assert with_shot.has_screenshot is True


# ── Staleness ─────────────────────────────────────────────────────────


class TestNonfunctionalRunOutdated:
    def test_current_run_is_not_outdated(self, db_session):
        _sprint, _requirement, run = _sprint_with_requirement(db_session)
        assert run.outdated_reasons == []
        assert run.outdated is False

    def test_each_revision_fires_its_own_reason(self, db_session):
        for field, reason in (
            ("requirement_revision", "requirement"),
            ("plan_revision", "test_plan"),
            ("env_revision", "test_environment"),
        ):
            _sprint, _requirement, run = _sprint_with_requirement(db_session, **{field: -1})
            assert run.outdated_reasons == [reason], field
            assert run.outdated is True

    def test_an_archived_requirement_reads_as_outdated(self, db_session):
        _sprint, requirement, run = _sprint_with_requirement(db_session)
        requirement.archived = True
        db_session.add(requirement)
        db_session.commit()
        db_session.refresh(run)

        assert "requirement" in run.outdated_reasons
        assert run.requirement_deleted is True


# ── Sprint flag ───────────────────────────────────────────────────────


class TestSprintHasNonfunctionalRuns:
    def test_false_with_no_runs(self, db_session):
        sprint = _seed_sprint(db_session)
        assert sprint.has_nonfunctional_runs is False

    def test_true_once_a_run_exists(self, db_session):
        sprint, _requirement, _run = _sprint_with_requirement(db_session)
        db_session.refresh(sprint)
        assert sprint.has_nonfunctional_runs is True


# ── Defect pool ───────────────────────────────────────────────────────


class TestDefectGroupPool:
    def test_a_group_defaults_to_the_functional_pool(self, db_session):
        from backend.models.database import DefectGroup

        sprint = _seed_sprint(db_session)
        group = DefectGroup(sprint_id=sprint.id, title="T", expected="E", actual="A")
        db_session.add(group)
        db_session.commit()
        db_session.refresh(group)

        assert group.pool == DefectPool.FUNCTIONAL


class TestLoadMethodSafety:
    def test_only_reads_are_safe_and_the_unknown_is_not(self):
        assert LoadMethod.is_safe("GET") is True
        assert LoadMethod.is_safe("head") is True
        assert LoadMethod.is_safe("OPTIONS") is True
        for method in ("POST", "PUT", "PATCH", "DELETE", "TRACE", "", None):
            assert LoadMethod.is_safe(method) is False, method
