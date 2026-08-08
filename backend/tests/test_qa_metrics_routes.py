"""Tests for GET /api/sprints/{id}/qa-metrics.

The counting rules themselves are pinned in ``test_qa_metrics.py`` against
hand-built objects; this module covers what only real rows can answer —
the response shape, the 404, and that the eager-load actually holds on an
endpoint the test-runs page polls.
"""

import pytest
from sqlalchemy import event
from sqlmodel import select

from backend.models.database import (
    DefectGroup,
    ExploratoryRunStatus,
    ExploratorySessionStatus,
    RequirementStatus,
    TestCaseExecution,
    TestCaseExecutionStatus,
    TestExecutionStatus,
    TestPlanStatus,
)
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


def _seed_plan_cases(db_session, requirement, count=2):
    """An approved plan and its cases — one per requirement, reused by re-runs."""
    plan = _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED)
    return [_seed_test_case(db_session, plan, position=i, title=f"Case {i}") for i in range(count)]


def _seed_completed_scripted_run(db_session, sprint, requirement, cases, *, bugs=0):
    """A completed run over *requirement*, with *bugs* of its cases failing."""
    run = _seed_test_run(db_session, sprint)
    execution = _seed_test_execution(
        db_session, run, requirement, status=TestExecutionStatus.COMPLETED
    )
    for i, case in enumerate(cases):
        failing = i < bugs
        _seed_test_case_execution(
            db_session,
            execution,
            case,
            status=(TestCaseExecutionStatus.FAILED if failing else TestCaseExecutionStatus.PASSED),
            finding_severity="high" if failing else None,
            finding_title=f"Defect {i}" if failing else None,
            finding_steps_to_reproduce="Do the thing" if failing else None,
            finding_expected="It works" if failing else None,
            finding_actual="It does not" if failing else None,
        )
    return run


@pytest.mark.asyncio
async def test_404_for_an_unknown_sprint(async_client):
    resp = await async_client.get("/api/sprints/99999/qa-metrics")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_a_sprint_with_no_runs_returns_zeros(async_client, db_session):
    sprint = _seed_sprint(db_session)
    _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)

    resp = await async_client.get(f"/api/sprints/{sprint.id}/qa-metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["sprint_id"] == sprint.id
    assert body["distinct_test_cases_run"] == 0
    assert body["bug_count"] == 0
    assert body["requirements_total"] == 1
    assert body["requirements_covered"] == 0
    assert body["bugs_per_requirement"] is None
    assert body["bugs_per_test_case"] is None
    assert body["per_requirement"] == []


@pytest.mark.asyncio
async def test_returns_the_expected_shape_for_a_seeded_sprint(async_client, db_session):
    sprint = _seed_sprint(db_session)
    scripted = _seed_requirement(
        db_session, sprint, status=RequirementStatus.CONFIRMED, name="Checkout"
    )
    explored = _seed_requirement(
        db_session, sprint, status=RequirementStatus.CONFIRMED, name="Login"
    )
    _seed_completed_scripted_run(
        db_session, sprint, scripted, _seed_plan_cases(db_session, scripted, 3), bugs=1
    )

    exploratory = _seed_exploratory_run(
        db_session, sprint, explored, status=ExploratoryRunStatus.COMPLETED
    )
    exploratory_session = _seed_exploratory_session(
        db_session, exploratory, status=ExploratorySessionStatus.COMPLETED
    )
    _seed_exploratory_finding(db_session, exploratory_session)

    resp = await async_client.get(f"/api/sprints/{sprint.id}/qa-metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["distinct_test_cases_run"] == 3
    assert body["case_executions"] == 3
    assert body["executions_passed"] == 2
    assert body["executions_failed"] == 1
    assert body["exploratory_sessions"] == 1
    assert body["requirements_explored"] == 1
    assert body["bug_count"] == 2
    assert body["high_severity_bug_count"] == 2
    assert body["requirements_covered"] == 2
    assert body["requirements_total"] == 2
    assert body["bugs_per_requirement"] == 1.0
    assert body["bugs_per_test_case"] == pytest.approx(2 / 3)
    assert {row["requirement_name"] for row in body["per_requirement"]} == {"Checkout", "Login"}
    assert body["excluded_runs_running"] == 0
    assert body["excluded_runs_failed"] == 0


@pytest.mark.asyncio
async def test_an_in_flight_run_is_excluded_and_named(async_client, db_session):
    sprint = _seed_sprint(db_session)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    plan = _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED)
    case = _seed_test_case(db_session, plan, position=0)
    run = _seed_test_run(db_session, sprint)
    execution = _seed_test_execution(
        db_session, run, requirement, status=TestExecutionStatus.RUNNING
    )
    _seed_test_case_execution(db_session, execution, case, status=TestCaseExecutionStatus.PENDING)

    resp = await async_client.get(f"/api/sprints/{sprint.id}/qa-metrics")

    body = resp.json()
    assert body["excluded_runs_running"] == 1
    assert body["case_executions"] == 0


@pytest.mark.asyncio
async def test_the_breakdown_names_an_archived_requirement(async_client, db_session):
    sprint = _seed_sprint(db_session)
    requirement = _seed_requirement(
        db_session, sprint, status=RequirementStatus.CONFIRMED, name="Removed feature"
    )
    _seed_completed_scripted_run(
        db_session, sprint, requirement, _seed_plan_cases(db_session, requirement, 1), bugs=1
    )
    requirement.archived = True
    db_session.add(requirement)
    db_session.commit()

    resp = await async_client.get(f"/api/sprints/{sprint.id}/qa-metrics")

    rows = resp.json()["per_requirement"]
    assert len(rows) == 1
    assert rows[0]["requirement_name"] == "Removed feature"
    assert rows[0]["requirement_deleted"] is True


@pytest.mark.asyncio
async def test_grouped_findings_collapse_without_reading_the_group_table(async_client, db_session):
    """Two differently-worded findings in one ``DefectGroup`` are one bug.

    And the endpoint never queries ``defectgroup`` to work that out: it
    counts a raw column on rows it already eager-loads.  That is why this
    needed no new ``selectinload`` — and why the panel gets counts rather
    than representative text.
    """
    sprint = _seed_sprint(db_session)
    requirement = _seed_requirement(
        db_session, sprint, status=RequirementStatus.CONFIRMED, name="Checkout"
    )
    _seed_completed_scripted_run(
        db_session, sprint, requirement, _seed_plan_cases(db_session, requirement, 2), bugs=2
    )
    group = DefectGroup(
        sprint_id=sprint.id, title="Defect 0", expected="It works", actual="It does not"
    )
    db_session.add(group)
    db_session.commit()
    for case in db_session.exec(select(TestCaseExecution)).all():
        case.defect_group_id = group.id
        db_session.add(case)
    db_session.commit()
    db_session.expire_all()

    statements: list[str] = []
    engine = db_session.get_bind()

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    resp = await async_client.get(f"/api/sprints/{sprint.id}/qa-metrics")
    event.remove(engine, "before_cursor_execute", _record)

    assert resp.status_code == 200
    assert resp.json()["bug_count"] == 1
    assert not [s for s in statements if "defectgroup" in s.lower()]


@pytest.mark.asyncio
async def test_query_count_is_flat_in_run_count(async_client, db_session):
    """Guards the eager-load: this endpoint is polled every 2.5 s.

    Asserted as "more runs must not mean more queries" rather than against
    a fixed number, since that is the property that matters and a literal
    would break on any unrelated query the route gains.
    """
    counts = {}
    for run_count in (1, 4):
        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(
            db_session, sprint, status=RequirementStatus.CONFIRMED, name="Checkout"
        )
        cases = _seed_plan_cases(db_session, requirement, 2)
        for _ in range(run_count):
            _seed_completed_scripted_run(db_session, sprint, requirement, cases)
            exploratory = _seed_exploratory_run(
                db_session, sprint, requirement, status=ExploratoryRunStatus.COMPLETED
            )
            exploratory_session = _seed_exploratory_session(
                db_session, exploratory, status=ExploratorySessionStatus.COMPLETED
            )
            _seed_exploratory_finding(db_session, exploratory_session)
        db_session.expire_all()

        counter = {"n": 0}
        engine = db_session.get_bind()

        @event.listens_for(engine, "before_cursor_execute")
        def _count(conn, cursor, statement, params, context, executemany, _c=counter):
            _c["n"] += 1

        resp = await async_client.get(f"/api/sprints/{sprint.id}/qa-metrics")
        event.remove(engine, "before_cursor_execute", _count)

        assert resp.status_code == 200
        counts[run_count] = counter["n"]

    assert counts[1] == counts[4], (
        f"query count grew with run count: {counts} — the selectinload chains "
        "are not covering everything the aggregator walks"
    )
