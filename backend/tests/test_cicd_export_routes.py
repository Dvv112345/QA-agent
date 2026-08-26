"""Export create / list / get / restart — every refusal happens before the row exists."""

import json

import pytest
from sqlmodel import select

from backend.models.database import (
    CicdConfig,
    CicdExport,
    CicdExportStatus,
    CicdProvider,
    Requirement,
    RequirementStatus,
    TestCase,
    TestEnvironmentAccess,
    TestEnvironmentStatus,
    TestPlan,
    TestPlanStatus,
)
from backend.tests.test_requirement_routes import _seed_sprint

_ENV_VARS = json.dumps({"BASE_URL": "https://app.test", "QA_PASSWORD": "hunter2secret"})


def _seed_exportable(db_session, *, active: bool = True, script: str | None = "print(1)"):
    """A sprint with a CI/CD config, an environment, and one exportable case."""
    sprint = _seed_sprint(db_session, active=active)
    db_session.add(
        CicdConfig(
            sprint_id=sprint.id,
            provider=CicdProvider.GITHUB_ACTIONS,
            access_token="encrypted",
        )
    )
    db_session.add(
        TestEnvironmentAccess(
            sprint_id=sprint.id,
            content="staging",
            original_content="staging",
            status=TestEnvironmentStatus.CONFIRMED,
            env_vars_json=_ENV_VARS,
        )
    )
    db_session.commit()

    requirement = Requirement(
        sprint_id=sprint.id,
        name="Login",
        description="d",
        original_description="d",
        status=RequirementStatus.CONFIRMED,
    )
    db_session.add(requirement)
    db_session.commit()
    plan = TestPlan(requirement_id=requirement.id, status=TestPlanStatus.APPROVED)
    db_session.add(plan)
    db_session.commit()
    case = TestCase(
        test_plan_id=plan.id,
        position=1,
        title="Happy path",
        steps="s",
        expected_result="e",
        case_type="functional",
        priority="high",
        script=script,
        script_requirement_revision=requirement.content_revision,
        script_plan_revision=plan.content_revision,
        script_env_revision=sprint.test_environment.content_revision,
    )
    db_session.add(case)
    db_session.commit()
    db_session.refresh(sprint)
    return sprint, case


def _settle(db_session, export_id: int, status: str = CicdExportStatus.COMPLETED) -> CicdExport:
    """Take an export out of flight, the way its job would."""
    export = db_session.get(CicdExport, export_id)
    export.status = status
    db_session.add(export)
    db_session.commit()
    return export


# ── Create ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_records_the_selection_and_returns_pending(async_client, db_session):
    sprint, case = _seed_exportable(db_session)

    resp = await async_client.post(
        f"/api/sprints/{sprint.id}/cicd-exports", json={"test_case_ids": [case.id]}
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == CicdExportStatus.PENDING
    assert body["provider"] == CicdProvider.GITHUB_ACTIONS
    assert body["case_count"] == 0  # receipts are written only on success

    stored = db_session.exec(select(CicdExport)).one()
    assert stored.selected_case_ids == [case.id]


@pytest.mark.asyncio
async def test_create_defaults_to_every_eligible_case(async_client, db_session):
    sprint, case = _seed_exportable(db_session)

    resp = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})

    assert resp.status_code == 201
    assert db_session.exec(select(CicdExport)).one().selected_case_ids == [case.id]


@pytest.mark.asyncio
async def test_create_ignores_ineligible_ids_in_the_selection(async_client, db_session):
    """The job re-derives eligibility too — this is the earlier of two filters."""
    sprint, case = _seed_exportable(db_session)
    stale = TestCase(
        test_plan_id=case.test_plan_id,
        position=2,
        title="Stale",
        steps="s",
        expected_result="e",
        case_type="functional",
        priority="high",
        script="print(2)",
    )
    db_session.add(stale)
    db_session.commit()

    resp = await async_client.post(
        f"/api/sprints/{sprint.id}/cicd-exports",
        json={"test_case_ids": [case.id, stale.id]},
    )

    assert resp.status_code == 201
    assert db_session.exec(select(CicdExport)).one().selected_case_ids == [case.id]


@pytest.mark.asyncio
async def test_create_422s_without_a_config_and_creates_nothing(async_client, db_session):
    sprint, case = _seed_exportable(db_session)
    db_session.delete(sprint.cicd_config)
    db_session.commit()

    resp = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})

    assert resp.status_code == 422
    assert "Connect a CI/CD target" in resp.json()["detail"]
    assert db_session.exec(select(CicdExport)).all() == []


@pytest.mark.asyncio
async def test_create_422s_when_the_environment_has_no_variables(async_client, db_session):
    """A later insufficient check can clear them, and a finished sprint may export."""
    sprint, case = _seed_exportable(db_session)
    sprint.test_environment.env_vars_json = None
    db_session.add(sprint.test_environment)
    db_session.commit()

    resp = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})

    assert resp.status_code == 422
    assert "no environment to run against" in resp.json()["detail"]
    assert db_session.exec(select(CicdExport)).all() == []


@pytest.mark.asyncio
async def test_create_422s_when_every_case_is_ineligible_and_says_why(async_client, db_session):
    sprint, case = _seed_exportable(db_session, script=None)

    resp = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "never run to a verdict" in detail
    assert db_session.exec(select(CicdExport)).all() == []


@pytest.mark.asyncio
async def test_create_422s_when_the_selection_is_entirely_ineligible(async_client, db_session):
    sprint, case = _seed_exportable(db_session)
    case.script = None
    db_session.add(case)
    db_session.commit()

    resp = await async_client.post(
        f"/api/sprints/{sprint.id}/cicd-exports", json={"test_case_ids": [case.id]}
    )

    assert resp.status_code == 422
    assert db_session.exec(select(CicdExport)).all() == []


@pytest.mark.asyncio
async def test_create_succeeds_on_a_finished_sprint(async_client, db_session):
    """Exporting verified scripts is exactly what a finished sprint is for."""
    sprint, case = _seed_exportable(db_session, active=False)

    resp = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})

    assert resp.status_code == 201


@pytest.mark.parametrize("in_flight", [CicdExportStatus.PENDING, CicdExportStatus.RUNNING])
@pytest.mark.asyncio
async def test_create_is_refused_while_an_export_is_in_flight(async_client, db_session, in_flight):
    """A second click must not open a second pull request for the same scripts."""
    sprint, case = _seed_exportable(db_session)
    first = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})
    _settle(db_session, first.json()["id"], in_flight)

    second = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})

    assert second.status_code == 422
    assert "already in progress" in second.json()["detail"]
    assert [row.id for row in db_session.exec(select(CicdExport)).all()] == [first.json()["id"]]


@pytest.mark.asyncio
async def test_create_is_allowed_once_the_previous_export_settles(async_client, db_session):
    """The gate is one export at a time, not one export ever."""
    sprint, case = _seed_exportable(db_session)
    first = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})

    for status in (CicdExportStatus.COMPLETED, CicdExportStatus.FAILED):
        _settle(db_session, first.json()["id"], status)
        again = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})
        assert again.status_code == 201, again.text
        _settle(db_session, again.json()["id"])


@pytest.mark.asyncio
async def test_another_sprints_export_does_not_block_this_one(async_client, db_session):
    """The gate is per sprint — two sprints export to two different repositories."""
    busy_sprint, _ = _seed_exportable(db_session)
    other_sprint, _ = _seed_exportable(db_session)
    await async_client.post(f"/api/sprints/{busy_sprint.id}/cicd-exports", json={})

    resp = await async_client.post(f"/api/sprints/{other_sprint.id}/cicd-exports", json={})

    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_create_404s_for_an_unknown_sprint(async_client):
    resp = await async_client.post("/api/sprints/9999/cicd-exports", json={})

    assert resp.status_code == 404


# ── List and get ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_is_newest_first(async_client, db_session):
    sprint, case = _seed_exportable(db_session)
    first = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})
    _settle(db_session, first.json()["id"])
    second = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})

    resp = await async_client.get(f"/api/sprints/{sprint.id}/cicd-exports")

    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()]
    assert ids == [second.json()["id"], first.json()["id"]]


@pytest.mark.asyncio
async def test_get_returns_one_export_and_404s_for_an_unknown_id(async_client, db_session):
    sprint, case = _seed_exportable(db_session)
    created = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})
    export_id = created.json()["id"]

    found = await async_client.get(f"/api/cicd-exports/{export_id}")
    missing = await async_client.get("/api/cicd-exports/9999")

    assert found.status_code == 200
    assert found.json()["id"] == export_id
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_no_token_or_env_value_appears_in_any_export_response(async_client, db_session):
    sprint, case = _seed_exportable(db_session)

    created = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})
    listed = await async_client.get(f"/api/sprints/{sprint.id}/cicd-exports")

    for response in (created, listed):
        assert "hunter2secret" not in response.text
        assert "encrypted" not in response.text


# ── Restart ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_is_accepted_on_a_failed_export(async_client, db_session):
    sprint, case = _seed_exportable(db_session)
    created = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})
    export = db_session.get(CicdExport, created.json()["id"])
    export.status = CicdExportStatus.FAILED
    export.error = "GitHub was unreachable."
    export.retry_count = 3
    db_session.add(export)
    db_session.commit()

    resp = await async_client.post(f"/api/cicd-exports/{export.id}/restart")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == CicdExportStatus.PENDING
    assert body["error"] is None
    db_session.expire_all()
    assert db_session.get(CicdExport, export.id).retry_count == 0


@pytest.mark.asyncio
async def test_restart_is_refused_while_running(async_client, db_session):
    sprint, case = _seed_exportable(db_session)
    created = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})
    export = db_session.get(CicdExport, created.json()["id"])
    export.status = CicdExportStatus.RUNNING
    db_session.add(export)
    db_session.commit()

    resp = await async_client.post(f"/api/cicd-exports/{export.id}/restart")

    assert resp.status_code == 422
    assert "still running" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_restart_is_accepted_on_a_completed_export(async_client, db_session):
    """Uncapped, and a fresh branch every attempt — so a re-export is legitimate."""
    sprint, case = _seed_exportable(db_session)
    created = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})
    export = db_session.get(CicdExport, created.json()["id"])
    export.status = CicdExportStatus.COMPLETED
    db_session.add(export)
    db_session.commit()

    resp = await async_client.post(f"/api/cicd-exports/{export.id}/restart")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_restart_is_refused_while_a_sibling_export_is_in_flight(async_client, db_session):
    """Restart cannot open the second pull request that create refuses to."""
    sprint, case = _seed_exportable(db_session)
    first = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})
    failed = _settle(db_session, first.json()["id"], CicdExportStatus.FAILED)
    second = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})
    assert second.status_code == 201

    resp = await async_client.post(f"/api/cicd-exports/{failed.id}/restart")

    assert resp.status_code == 422
    assert "already in progress" in resp.json()["detail"]
    db_session.expire_all()
    assert db_session.get(CicdExport, failed.id).status == CicdExportStatus.FAILED


@pytest.mark.asyncio
async def test_restart_re_pends_an_export_that_is_only_blocked_by_itself(async_client, db_session):
    """`pending` is what a Redis outage leaves behind — restarting it must still work."""
    sprint, case = _seed_exportable(db_session)
    created = await async_client.post(f"/api/sprints/{sprint.id}/cicd-exports", json={})

    resp = await async_client.post(f"/api/cicd-exports/{created.json()['id']}/restart")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == CicdExportStatus.PENDING


@pytest.mark.asyncio
async def test_restart_404s_for_an_unknown_export(async_client):
    assert (await async_client.post("/api/cicd-exports/9999/restart")).status_code == 404
