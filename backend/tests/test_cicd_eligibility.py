"""Per-case export eligibility — the six outcomes, and the endpoint over them."""

import pytest
from sqlmodel import select

from backend.models.database import (
    CicdExport,
    CicdExportItem,
    CicdExportStatus,
    CicdProvider,
    Requirement,
    RequirementStatus,
    Sprint,
    TestCase,
    TestEnvironmentAccess,
    TestEnvironmentStatus,
    TestPlan,
    TestPlanStatus,
)
from backend.services import cicd_eligibility
from backend.tests.test_requirement_routes import _seed_sprint


def _seed_case(
    db_session, sprint: Sprint, *, script: str | None = "print(1)", **kwargs
) -> TestCase:
    """A confirmed requirement, an approved plan and one case on it."""
    requirement = Requirement(
        sprint_id=sprint.id,
        name=kwargs.pop("req_name", "Login"),
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
        title=kwargs.pop("title", "Happy path"),
        steps="s",
        expected_result="e",
        case_type="functional",
        priority="high",
        script=script,
        script_requirement_revision=kwargs.pop(
            "script_requirement_revision", requirement.content_revision
        ),
        script_plan_revision=kwargs.pop("script_plan_revision", plan.content_revision),
        script_env_revision=kwargs.pop("script_env_revision", 0),
        **kwargs,
    )
    db_session.add(case)
    db_session.commit()
    db_session.refresh(sprint)
    return case


def _entries(db_session, sprint: Sprint):
    return cicd_eligibility.case_entries(db_session, sprint)


# ── The per-case outcomes ─────────────────────────────────────────────


def test_a_current_cached_script_is_eligible(db_session):
    sprint = _seed_sprint(db_session)
    _seed_case(db_session, sprint)

    entry = _entries(db_session, sprint)[0]

    assert entry.eligible is True
    assert entry.reason is None
    assert entry.stale_reasons == []


def test_a_case_with_no_script_is_no_script(db_session):
    sprint = _seed_sprint(db_session)
    _seed_case(db_session, sprint, script=None)

    entry = _entries(db_session, sprint)[0]

    assert entry.eligible is False
    assert entry.reason == "no_script"


def test_a_case_whose_requirement_moved_is_stale(db_session):
    sprint = _seed_sprint(db_session)
    case = _seed_case(db_session, sprint)
    requirement = db_session.get(Requirement, case.test_plan.requirement_id)
    requirement.content_revision += 1
    db_session.add(requirement)
    db_session.commit()
    db_session.refresh(sprint)

    entry = _entries(db_session, sprint)[0]

    assert entry.eligible is False
    assert entry.reason == "stale"
    assert "requirement" in entry.stale_reasons


def test_a_case_whose_plan_moved_is_stale(db_session):
    sprint = _seed_sprint(db_session)
    case = _seed_case(db_session, sprint)
    plan = db_session.get(TestPlan, case.test_plan_id)
    plan.content_revision += 1
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(sprint)

    entry = _entries(db_session, sprint)[0]

    assert entry.reason == "stale"
    assert "test_plan" in entry.stale_reasons


def test_a_case_whose_environment_moved_is_stale(db_session):
    sprint = _seed_sprint(db_session)
    env = TestEnvironmentAccess(
        sprint_id=sprint.id,
        content="reachable at https://staging.example.com",
        original_content="reachable at https://staging.example.com",
        status=TestEnvironmentStatus.CONFIRMED,
        content_revision=4,
    )
    db_session.add(env)
    db_session.commit()
    _seed_case(db_session, sprint, script_env_revision=3)
    db_session.refresh(sprint)

    entry = _entries(db_session, sprint)[0]

    assert entry.reason == "stale"
    assert "test_environment" in entry.stale_reasons


@pytest.mark.parametrize(
    "column",
    ["script_requirement_revision", "script_plan_revision", "script_env_revision"],
)
def test_a_null_revision_reads_as_stale_with_reason_unknown(db_session, column):
    """A script cached before the stamp existed — unknown collapses into stale."""
    sprint = _seed_sprint(db_session)
    _seed_case(db_session, sprint, **{column: None})

    entry = _entries(db_session, sprint)[0]

    assert entry.eligible is False
    assert entry.reason == "stale"
    assert entry.stale_reasons == [cicd_eligibility.UNKNOWN_REVISION]


def test_an_archived_case_does_not_appear_at_all(db_session):
    sprint = _seed_sprint(db_session)
    case = _seed_case(db_session, sprint)
    case.archived = True
    db_session.add(case)
    db_session.commit()
    db_session.refresh(sprint)

    assert _entries(db_session, sprint) == []


def test_a_requirement_with_no_plan_contributes_no_rows(db_session):
    sprint = _seed_sprint(db_session)
    db_session.add(
        Requirement(
            sprint_id=sprint.id,
            name="No plan",
            description="d",
            original_description="d",
            status=RequirementStatus.CONFIRMED,
        )
    )
    db_session.commit()
    db_session.refresh(sprint)

    assert _entries(db_session, sprint) == []


def test_an_archived_requirement_contributes_no_rows(db_session):
    sprint = _seed_sprint(db_session)
    case = _seed_case(db_session, sprint)
    requirement = db_session.get(Requirement, case.test_plan.requirement_id)
    requirement.archived = True
    db_session.add(requirement)
    db_session.commit()
    db_session.refresh(sprint)

    assert _entries(db_session, sprint) == []


# ── Export history ────────────────────────────────────────────────────


def _seed_export(db_session, sprint, case, *, status, pr_url):
    export = CicdExport(
        sprint_id=sprint.id, provider=CicdProvider.GITHUB_ACTIONS, status=status, pr_url=pr_url
    )
    export.items = [
        CicdExportItem(
            test_case_id=case.id,
            case_title=case.title,
            requirement_name="Login",
            committed_path="qa-agent-tests/login_1/happy_1.py",
        )
    ]
    db_session.add(export)
    db_session.commit()
    return export


def test_previously_exported_is_true_only_after_a_completed_export(db_session):
    sprint = _seed_sprint(db_session)
    case = _seed_case(db_session, sprint)

    _seed_export(db_session, sprint, case, status=CicdExportStatus.FAILED, pr_url=None)
    db_session.refresh(sprint)
    assert _entries(db_session, sprint)[0].previously_exported is False

    _seed_export(
        db_session,
        sprint,
        case,
        status=CicdExportStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/3",
    )
    db_session.refresh(sprint)
    entry = _entries(db_session, sprint)[0]
    assert entry.previously_exported is True
    assert entry.last_export_pr_url == "https://github.com/owner/repo/pull/3"


def test_the_newest_completed_export_wins_the_pr_link(db_session):
    sprint = _seed_sprint(db_session)
    case = _seed_case(db_session, sprint)
    _seed_export(
        db_session, sprint, case, status=CicdExportStatus.COMPLETED, pr_url="https://x/pull/1"
    )
    _seed_export(
        db_session, sprint, case, status=CicdExportStatus.COMPLETED, pr_url="https://x/pull/2"
    )
    db_session.refresh(sprint)

    assert _entries(db_session, sprint)[0].last_export_pr_url == "https://x/pull/2"


def test_eligible_ids_selects_only_the_exportable_cases(db_session):
    sprint = _seed_sprint(db_session)
    good = _seed_case(db_session, sprint, title="Good")
    _seed_case(db_session, sprint, title="No script", req_name="Other", script=None)
    db_session.refresh(sprint)

    assert cicd_eligibility.eligible_ids(_entries(db_session, sprint)) == {good.id}


# ── The endpoint ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_endpoint_reports_counts_and_the_variable_secret_split(async_client, db_session):
    sprint = _seed_sprint(db_session)
    db_session.add(
        TestEnvironmentAccess(
            sprint_id=sprint.id,
            content="staging",
            original_content="staging",
            status=TestEnvironmentStatus.CONFIRMED,
            env_vars_json=(
                '{"BASE_URL": "https://staging.example.com", "API_URL": "http://api.local",'
                ' "QA_PASSWORD": "hunter2"}'
            ),
        )
    )
    db_session.commit()
    _seed_case(db_session, sprint, title="Ready")
    _seed_case(db_session, sprint, title="Unrun", req_name="Signup", script=None)

    resp = await async_client.get(f"/api/sprints/{sprint.id}/cicd-eligibility")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["eligible_count"] == 1
    assert body["no_script_count"] == 1
    assert body["stale_count"] == 0
    # Each entry names both sides: what the team creates in CI, and the
    # sprint variable it feeds. Here they coincide — nothing needed mapping.
    assert sorted(entry["name"] for entry in body["variable_names"]) == ["API_URL", "BASE_URL"]
    assert body["secret_names"] == [{"name": "QA_PASSWORD", "env_var": "QA_PASSWORD"}]
    # Names only — no environment value may appear anywhere in the payload.
    assert "hunter2" not in resp.text
    assert "staging.example.com" not in resp.text


@pytest.mark.asyncio
async def test_the_endpoint_names_the_ci_side_name_when_it_differs(async_client, db_session):
    """A name the CI system will not take verbatim must not be shown as-is.

    The page tells the team what to create; `base_url` is not what they
    create on GitHub Actions, `BASE_URL` is.
    """
    sprint = _seed_sprint(db_session)
    db_session.add(
        TestEnvironmentAccess(
            sprint_id=sprint.id,
            content="staging",
            original_content="staging",
            status=TestEnvironmentStatus.CONFIRMED,
            env_vars_json='{"base_url": "https://staging.example.com", "GITHUB_PAT": "ghp_x"}',
        )
    )
    db_session.commit()
    _seed_case(db_session, sprint)

    body = (await async_client.get(f"/api/sprints/{sprint.id}/cicd-eligibility")).json()

    assert body["variable_names"] == [{"name": "BASE_URL", "env_var": "base_url"}]
    # Actions reserves the GITHUB_ prefix, so this one is rewritten too.
    assert body["secret_names"] == [{"name": "QA_GITHUB_PAT", "env_var": "GITHUB_PAT"}]


@pytest.mark.asyncio
async def test_endpoint_answers_on_a_finished_sprint(async_client, db_session):
    sprint = _seed_sprint(db_session, active=False)
    _seed_case(db_session, sprint)

    resp = await async_client.get(f"/api/sprints/{sprint.id}/cicd-eligibility")

    assert resp.status_code == 200
    assert resp.json()["eligible_count"] == 1


@pytest.mark.asyncio
async def test_endpoint_404s_for_an_unknown_sprint(async_client):
    assert (await async_client.get("/api/sprints/9999/cicd-eligibility")).status_code == 404


@pytest.mark.asyncio
async def test_endpoint_reports_empty_name_lists_when_no_environment_exists(
    async_client, db_session
):
    sprint = _seed_sprint(db_session)
    _seed_case(db_session, sprint)

    body = (await async_client.get(f"/api/sprints/{sprint.id}/cicd-eligibility")).json()

    assert body["variable_names"] == []
    assert body["secret_names"] == []


@pytest.mark.asyncio
async def test_endpoint_lists_ineligible_rows_rather_than_hiding_them(async_client, db_session):
    sprint = _seed_sprint(db_session)
    _seed_case(db_session, sprint, title="Unrun", script=None)

    entries = (await async_client.get(f"/api/sprints/{sprint.id}/cicd-eligibility")).json()[
        "entries"
    ]

    assert len(entries) == 1
    assert entries[0]["case_title"] == "Unrun"
    assert entries[0]["eligible"] is False
    assert entries[0]["reason"] == "no_script"


def test_no_export_rows_means_no_history_query_result(db_session):
    """Sanity: the history lookup is empty rather than raising on a fresh sprint."""
    sprint = _seed_sprint(db_session)
    _seed_case(db_session, sprint)

    assert db_session.exec(select(CicdExport)).all() == []
    assert _entries(db_session, sprint)[0].previously_exported is False
