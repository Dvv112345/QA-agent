"""Tests for backend/services/finding_grouping.py — the assignment pass.

``llm.group_findings`` is stubbed throughout: most tests make it *raise*,
so that anything reaching it is a test failure rather than a silent
network call, and the deterministic prefilter is what runs.  The tests
that exercise the paraphrase path answer with fixed groups instead.
"""

import pytest
from sqlmodel import select

from backend.models.database import (
    DefectGroup,
    ExploratoryRunStatus,
    FindingType,
    RequirementStatus,
    TestCaseExecution,
    TestCaseExecutionStatus,
)
from backend.services import finding_dedup, finding_grouping, llm
from backend.services.llm import FindingGroupingResult, FindingGroupItem
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import (
    _seed_exploratory_finding,
    _seed_exploratory_run,
    _seed_exploratory_session,
    _seed_test_case,
    _seed_test_case_execution,
    _seed_test_execution,
    _seed_test_plan,
    _seed_test_run,
)

# ── Stubs ─────────────────────────────────────────────────────────────


class _GroupStub:
    """Answers with fixed groups, or raises, and records every call."""

    def __init__(self, groups=None, error: Exception | None = None):
        self.groups = groups or []
        self.error = error
        self.calls: list = []

    def __call__(self, candidates, known):
        self.calls.append((candidates, known))
        if self.error is not None:
            raise self.error
        return FindingGroupingResult(groups=[FindingGroupItem(**group) for group in self.groups])


@pytest.fixture
def no_llm(monkeypatch):
    """The LLM stage must not be reached; the prefilter answers alone."""
    stub = _GroupStub(error=llm.LLMError("stubbed out — prefilter only"))
    monkeypatch.setattr(llm, "group_findings", stub)
    return stub


# ── Seeding ───────────────────────────────────────────────────────────


def _finding_fields(title="Checkout returns 500", severity="high", **overrides):
    fields = {
        "finding_severity": severity,
        "finding_title": title,
        "finding_steps_to_reproduce": "Open /checkout\nSubmit",
        "finding_expected": "The order is created",
        "finding_actual": "HTTP 500",
        "environment": "Windows-10 · Python 3.12.4",
    }
    fields.update(overrides)
    return fields


def _scripted_run(
    db_session,
    sprint=None,
    *,
    case_count=1,
    status=TestCaseExecutionStatus.FAILED,
    findings=None,
    requirement=None,
):
    """A scripted execution carrying *case_count* findings.

    *findings* overrides the finding fields per case, so a test can give
    two cases genuinely different wording.
    """
    sprint = sprint or _seed_sprint(db_session)
    requirement = requirement or _seed_requirement(
        db_session, sprint, status=RequirementStatus.CONFIRMED
    )
    plan = requirement.test_plan or _seed_test_plan(db_session, requirement)
    run = _seed_test_run(db_session, sprint)
    execution = _seed_test_execution(db_session, run, requirement)
    for index in range(case_count):
        case = _seed_test_case(db_session, plan, position=index)
        extra = (findings or [{}] * case_count)[index]
        _seed_test_case_execution(
            db_session,
            execution,
            case,
            status=status,
            **(_finding_fields(**extra) if status != TestCaseExecutionStatus.PASSED else {}),
        )
    db_session.refresh(execution)
    return sprint, execution


def _exploratory_run(db_session, sprint=None, *, finding_count=1, **finding_kwargs):
    sprint = sprint or _seed_sprint(db_session)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    run = _seed_exploratory_run(
        db_session, sprint, requirement, status=ExploratoryRunStatus.COMPLETED
    )
    exploratory_session = _seed_exploratory_session(db_session, run)
    for index in range(finding_count):
        _seed_exploratory_finding(db_session, exploratory_session, position=index, **finding_kwargs)
    db_session.refresh(run)
    return sprint, run


def _groups(db_session, sprint):
    return db_session.exec(
        select(DefectGroup).where(DefectGroup.sprint_id == sprint.id).order_by(DefectGroup.id)
    ).all()


# ── The model's own shape (Phase 1's columns, exercised here) ─────────


def test_defect_group_is_reachable_from_its_sprint(db_session):
    sprint = _seed_sprint(db_session)
    group = DefectGroup(
        sprint_id=sprint.id, title="Checkout returns 500", expected="Order", actual="500"
    )
    db_session.add(group)
    db_session.commit()
    db_session.refresh(sprint)

    assert [g.id for g in sprint.defect_groups] == [group.id]


def test_both_carriers_start_ungrouped(db_session):
    """The column defaults to null, which is what the fallback in
    ``qa_metrics`` reads as 'this row was never grouped'."""
    _, execution = _scripted_run(db_session)
    _, run = _exploratory_run(db_session)

    assert execution.cases[0].defect_group_id is None
    assert run.sessions[0].findings[0].defect_group_id is None


# ── The fast exit ─────────────────────────────────────────────────────


def _statements(db_session, monkeypatch) -> list[str]:
    """Every statement the pass executes through ``session.exec``."""
    recorded: list[str] = []
    original = db_session.exec

    def spy(statement, *args, **kwargs):
        recorded.append(str(statement))
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "exec", spy)
    return recorded


def test_a_run_with_no_bug_findings_costs_nothing(db_session, no_llm, monkeypatch):
    """No LLM call *and* no sprint-wide read.

    The fast exit sits before the known-defect query, not merely inside
    ``group_findings``' own empty-input guard — by the time that fired,
    the query building its argument would already have run.
    """
    _, execution = _scripted_run(db_session, status=TestCaseExecutionStatus.PASSED)
    statements = _statements(db_session, monkeypatch)

    finding_grouping.assign_defect_groups(db_session, execution)

    assert no_llm.calls == []
    assert not [s for s in statements if "defectgroup" in s.lower()]


def test_a_run_with_only_issue_findings_is_a_no_op(db_session, no_llm, monkeypatch):
    """An issue says testing was obstructed, not that the product is
    wrong — there is no defect for it to be an occurrence of."""
    sprint, execution = _scripted_run(db_session, status=TestCaseExecutionStatus.ERROR)
    statements = _statements(db_session, monkeypatch)

    finding_grouping.assign_defect_groups(db_session, execution)

    assert execution.cases[0].finding_type == FindingType.ISSUE
    assert no_llm.calls == []
    assert not [s for s in statements if "defectgroup" in s.lower()]
    assert _groups(db_session, sprint) == []


def test_the_sprint_row_is_locked_before_the_known_defects_are_read(
    db_session, no_llm, monkeypatch
):
    """Ordering is the only thing a test here can pin.

    The suite is single-threaded on SQLite, where ``with_for_update()``
    renders nothing, so no behavioural test can reach the race. Without
    this, the lock could be deleted — or moved below the read, which is
    the bug it exists to prevent — with every test still green.
    """
    _, execution = _scripted_run(db_session)
    statements = _statements(db_session, monkeypatch)

    finding_grouping.assign_defect_groups(db_session, execution)

    locks = [i for i, s in enumerate(statements) if "FROM sprint" in s and "FOR UPDATE" in s]
    reads = [i for i, s in enumerate(statements) if "FROM defectgroup" in s]
    assert locks and reads, statements
    assert locks[0] < reads[0]


def test_the_lock_renders_for_update_on_postgresql():
    """``with_for_update()`` is dialect-portable with no branch in our
    code: PostgreSQL gets the lock, SQLite silently omits it."""
    from sqlalchemy.dialects import postgresql, sqlite

    from backend.models.database import Sprint

    statement = select(Sprint).where(Sprint.id == 1).with_for_update()

    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" not in str(statement.compile(dialect=sqlite.dialect()))


# ── Grouping within one run ───────────────────────────────────────────


def test_identical_findings_collapse_without_an_llm_call(db_session, no_llm):
    """The common case — one broken dependency failing every case in a
    plan with the same words — costs nothing but the prefilter."""
    sprint, execution = _scripted_run(db_session, case_count=3)

    finding_grouping.assign_defect_groups(db_session, execution)

    groups = _groups(db_session, sprint)
    assert len(groups) == 1
    assert no_llm.calls == []
    assert {case.defect_group_id for case in execution.cases} == {groups[0].id}


def test_distinct_findings_open_distinct_groups(db_session, no_llm):
    sprint, execution = _scripted_run(
        db_session,
        case_count=2,
        findings=[{}, {"title": "Search returns nothing", "finding_actual": "Empty list"}],
    )

    finding_grouping.assign_defect_groups(db_session, execution)

    assert len(_groups(db_session, sprint)) == 2


def test_exploratory_findings_are_grouped_too(db_session, no_llm):
    sprint, run = _exploratory_run(db_session, finding_count=2)

    finding_grouping.assign_defect_groups(db_session, run)

    groups = _groups(db_session, sprint)
    assert len(groups) == 1
    findings = run.sessions[0].findings
    assert {f.defect_group_id for f in findings} == {groups[0].id}


def test_the_frozen_text_is_the_elected_representatives(db_session, no_llm):
    """Highest severity, then lowest position — elected in code, never by
    the model, because this text is what every later run is shown.

    The two reports normalize identically (``dedup_key`` drops digits, so
    a generated order id differs per case while the defect does not) but
    read differently, which is what makes "whose wording?" answerable.
    """
    sprint, execution = _scripted_run(
        db_session,
        case_count=2,
        findings=[
            {"severity": "low", "finding_actual": "HTTP 500 on order 8814"},
            {"severity": "high", "finding_actual": "HTTP 500 on order 9021"},
        ],
    )
    assert finding_dedup.dedup_key("Checkout returns 500", "x", "HTTP 500 on order 8814") == (
        finding_dedup.dedup_key("Checkout returns 500", "x", "HTTP 500 on order 9021")
    )

    finding_grouping.assign_defect_groups(db_session, execution)

    (group,) = _groups(db_session, sprint)
    assert group.actual == "HTTP 500 on order 9021"  # the high-severity report


# ── Grouping across runs ──────────────────────────────────────────────


def test_a_second_run_joins_an_existing_group_when_the_model_says_so(db_session, monkeypatch):
    """The paraphrase path: different words, one defect."""
    sprint, first = _scripted_run(db_session)
    monkeypatch.setattr(llm, "group_findings", _GroupStub())
    finding_grouping.assign_defect_groups(db_session, first)
    (group,) = _groups(db_session, sprint)

    _, second = _scripted_run(
        db_session,
        sprint,
        findings=[{"title": "The order endpoint errors on submit", "finding_actual": "Server 500"}],
    )
    stub = _GroupStub(groups=[{"indices": [0], "existing_key": str(group.id)}])
    monkeypatch.setattr(llm, "group_findings", stub)

    finding_grouping.assign_defect_groups(db_session, second)

    assert len(_groups(db_session, sprint)) == 1
    assert second.cases[0].defect_group_id == group.id
    # The known defect really was offered as a match target.
    assert [d.key for d in stub.calls[0][1]] == [str(group.id)]


def test_a_second_run_opens_a_new_group_when_the_model_says_null(db_session, monkeypatch):
    sprint, first = _scripted_run(db_session)
    monkeypatch.setattr(llm, "group_findings", _GroupStub())
    finding_grouping.assign_defect_groups(db_session, first)

    _, second = _scripted_run(
        db_session, sprint, findings=[{"title": "Search returns nothing", "finding_actual": "None"}]
    )
    monkeypatch.setattr(llm, "group_findings", _GroupStub(groups=[{"indices": [0]}]))

    finding_grouping.assign_defect_groups(db_session, second)

    assert len(_groups(db_session, sprint)) == 2


def test_a_regression_rejoins_its_original_group(db_session, no_llm):
    """Run 1 finds it, run 2 is clean, run 3 finds it again.

    The ordinary cross-run join — and the reason nothing anywhere has to
    handle a group whose members were all fixed: a group cannot lose one.
    """
    sprint, first = _scripted_run(db_session)
    finding_grouping.assign_defect_groups(db_session, first)
    (group,) = _groups(db_session, sprint)

    _, clean = _scripted_run(db_session, sprint, status=TestCaseExecutionStatus.PASSED)
    finding_grouping.assign_defect_groups(db_session, clean)

    _, third = _scripted_run(db_session, sprint)
    finding_grouping.assign_defect_groups(db_session, third)

    assert len(_groups(db_session, sprint)) == 1
    assert third.cases[0].defect_group_id == group.id


def test_a_higher_severity_member_does_not_rewrite_the_group(db_session, no_llm):
    """Append-only: membership grows, the record never changes under it."""
    sprint, first = _scripted_run(db_session, findings=[{"severity": "low"}])
    finding_grouping.assign_defect_groups(db_session, first)
    (group,) = _groups(db_session, sprint)
    created_at, title = group.created_at, group.title

    _, second = _scripted_run(db_session, sprint, findings=[{"severity": "high"}])
    finding_grouping.assign_defect_groups(db_session, second)

    (group,) = _groups(db_session, sprint)
    assert (group.created_at, group.title) == (created_at, title)


def test_re_running_the_pass_changes_nothing(db_session, no_llm):
    """Idempotent across a restart, because grouped rows are skipped."""
    sprint, execution = _scripted_run(db_session, case_count=2)
    finding_grouping.assign_defect_groups(db_session, execution)
    before = [(case.id, case.defect_group_id) for case in execution.cases]

    finding_grouping.assign_defect_groups(db_session, execution)

    assert len(_groups(db_session, sprint)) == 1
    assert [(case.id, case.defect_group_id) for case in execution.cases] == before


# ── Degradation ───────────────────────────────────────────────────────


def test_an_existing_key_naming_no_group_opens_a_new_one(db_session, monkeypatch):
    """Never written through as a dangling foreign key."""
    sprint, execution = _scripted_run(db_session)
    monkeypatch.setattr(llm, "group_findings", _GroupStub(groups=[{"indices": [0]}]))
    finding_grouping.assign_defect_groups(db_session, execution)

    _, second = _scripted_run(
        db_session, sprint, findings=[{"title": "Unrelated", "finding_actual": "Nothing"}]
    )
    monkeypatch.setattr(
        llm, "group_findings", _GroupStub(groups=[{"indices": [0], "existing_key": "9999"}])
    )

    finding_grouping.assign_defect_groups(db_session, second)

    groups = _groups(db_session, sprint)
    assert len(groups) == 2
    assert second.cases[0].defect_group_id in {g.id for g in groups}


def test_an_llm_error_degrades_to_the_prefilters_answer(db_session, no_llm):
    """A worse grouping, never no grouping — the deterministic pass is
    the floor, not the fallback of last resort."""
    sprint, execution = _scripted_run(db_session, case_count=2)

    finding_grouping.assign_defect_groups(db_session, execution)

    assert len(_groups(db_session, sprint)) == 1
    assert all(case.defect_group_id is not None for case in execution.cases)


def test_an_unexpected_failure_leaves_every_row_ungrouped(db_session, monkeypatch):
    """Never raises into a run that already finished; ``qa_metrics``
    falls back to text identity for rows the pass never reached."""
    sprint, execution = _scripted_run(db_session, case_count=2)

    def explode(session, sprint_row):
        raise RuntimeError("boom")

    monkeypatch.setattr(finding_grouping, "sprint_for", explode)

    finding_grouping.assign_defect_groups(db_session, execution)  # must not raise

    db_session.expire_all()
    cases = db_session.exec(select(TestCaseExecution)).all()
    assert [case.defect_group_id for case in cases] == [None, None]
    assert _groups(db_session, sprint) == []


def test_an_unknown_parent_type_is_a_no_op(db_session, no_llm):
    finding_grouping.assign_defect_groups(db_session, object())  # must not raise

    assert no_llm.calls == []


# ── Pools ═════════════════════════════════════════════════════════════


def _seed_nonfunctional(db_session, sprint, requirement, *, title="Images lack alt text", count=1):
    from backend.models.database import NonfunctionalRunStatus
    from backend.tests.test_nonfunctional_models import (
        _seed_nonfunctional_finding,
        _seed_nonfunctional_run,
        _seed_nonfunctional_target,
    )

    run = _seed_nonfunctional_run(
        db_session, sprint, requirement, status=NonfunctionalRunStatus.COMPLETED
    )
    target = _seed_nonfunctional_target(db_session, run)
    for index in range(count):
        _seed_nonfunctional_finding(db_session, target, position=index, title=title)
    db_session.refresh(run)
    return run


class TestDefectPools:
    """Functional and nonfunctional defects never join each other's groups."""

    def test_a_new_nonfunctional_group_is_stamped_with_its_pool(self, db_session, no_llm):
        from backend.models.database import DefectPool

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        run = _seed_nonfunctional(db_session, sprint, requirement)

        finding_grouping.assign_defect_groups(db_session, run)

        groups = db_session.exec(select(DefectGroup)).all()
        assert len(groups) == 1
        assert groups[0].pool == DefectPool.NONFUNCTIONAL

    def test_identical_text_in_the_other_pool_is_not_matched(self, db_session, no_llm):
        """The prefilter would otherwise collapse them — the pool filter is
        what keeps two different kinds of defect apart."""
        from backend.models.database import DefectPool

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        execution = _scripted_run(
            db_session, sprint, requirement=requirement, findings=[{"title": "Same text"}]
        )[1]
        run = _seed_nonfunctional(db_session, sprint, requirement, title="Same text")

        finding_grouping.assign_defect_groups(db_session, execution)
        finding_grouping.assign_defect_groups(db_session, run)

        groups = db_session.exec(select(DefectGroup)).all()
        assert len(groups) == 2
        assert {group.pool for group in groups} == {
            DefectPool.FUNCTIONAL,
            DefectPool.NONFUNCTIONAL,
        }

    def test_a_pre_existing_backfilled_group_is_still_matched(self, db_session, no_llm):
        """A group written before pools existed reads as `functional`."""
        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        first = _scripted_run(db_session, sprint, requirement=requirement)[1]
        finding_grouping.assign_defect_groups(db_session, first)
        existing = db_session.exec(select(DefectGroup)).one()

        second = _scripted_run(db_session, sprint)[1]
        finding_grouping.assign_defect_groups(db_session, second)

        assert len(db_session.exec(select(DefectGroup)).all()) == 1
        db_session.expire_all()
        assert all(
            case.defect_group_id == existing.id
            for case in db_session.exec(select(TestCaseExecution)).all()
            if case.finding_title
        )

    def test_two_nonfunctional_runs_share_one_group(self, db_session, no_llm):
        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        first = _seed_nonfunctional(db_session, sprint, requirement)
        second = _seed_nonfunctional(db_session, sprint, requirement)

        finding_grouping.assign_defect_groups(db_session, first)
        finding_grouping.assign_defect_groups(db_session, second)

        assert len(db_session.exec(select(DefectGroup)).all()) == 1

    def test_the_pass_is_idempotent_for_the_third_mode_too(self, db_session, no_llm):
        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        run = _seed_nonfunctional(db_session, sprint, requirement)

        finding_grouping.assign_defect_groups(db_session, run)
        finding_grouping.assign_defect_groups(db_session, run)

        assert len(db_session.exec(select(DefectGroup)).all()) == 1


# ── The dispatch registry ═════════════════════════════════════════════


class TestRunKindRegistry:
    """One table, so a new run mode cannot be half-added."""

    def test_every_exportable_kind_walks_resolves_and_exports(self, db_session):
        """The three job-owned levels, parametrized over the registry itself."""
        from backend.services import finding_export, findings

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        parents = {
            "scripted": _scripted_run(db_session, sprint, requirement=requirement)[1],
            "exploratory": _exploratory_run(db_session, sprint)[1],
            "nonfunctional": _seed_nonfunctional(db_session, sprint, requirement),
        }

        exportable = [kind for kind in findings.RUN_KINDS if kind.exportable]
        assert {kind.source_kind for kind in exportable} == set(parents)

        for kind in exportable:
            parent = parents[kind.source_kind]
            assert list(findings.iter_findings(parent, bugs_only=True)), kind.source_kind
            assert findings.sprint_for(parent) is not None, kind.source_kind
            spec = finding_export._spec_for(parent)
            assert spec is not None, kind.source_kind
            assert spec.source_kind == kind.source_kind
            assert spec.run_label

    def test_an_unknown_parent_answers_none_everywhere(self, db_session):
        from backend.services import finding_export, findings

        assert findings.kind_for(object()) is None
        assert list(findings.iter_findings(object())) == []
        assert findings.sprint_for(object()) is None
        assert finding_export._spec_for(object()) is None

    def test_the_pool_is_derived_in_exactly_one_place(self, db_session):
        """Both consumers read `RunKind.pool` — see the reference_map gotcha."""
        from backend.models.database import DefectPool
        from backend.services import findings

        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        run = _seed_nonfunctional(db_session, sprint, requirement)
        execution = _scripted_run(db_session, sprint, requirement=requirement)[1]

        assert all(f.pool == DefectPool.NONFUNCTIONAL for f in findings.iter_findings(run))
        assert all(f.pool == DefectPool.FUNCTIONAL for f in findings.iter_findings(execution))
