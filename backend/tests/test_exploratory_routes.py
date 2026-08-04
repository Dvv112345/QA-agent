"""Tests for backend/routes/exploratory.py."""

import json

import pytest

from backend.models.database import (
    ExploratoryRun,
    ExploratoryRunStatus,
    ExploratorySession,
    ExploratorySessionStatus,
    FindingSeverity,
    FindingType,
    RequirementStatus,
    TestCase,
    TestCasePriority,
    TestEnvironmentAccess,
    TestEnvironmentStatus,
    TestPlan,
    TestPlanStatus,
)
from backend.services import llm
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import (
    _seed_exploratory_finding,
    _seed_exploratory_run,
    _seed_exploratory_session,
)
from backend.tests.test_test_execution_routes import _RefreshStub

ENV_VARS = {"APP_URL": "https://app.test", "API_URL": "https://api.test", "PW": "hunter2"}


# ── seeding ───────────────────────────────────────────────────────────


def _seed_environment(db_session, sprint, env_vars=None, status=None):
    row = TestEnvironmentAccess(
        sprint_id=sprint.id,
        content="Access at https://app.test",
        original_content="Access at https://app.test",
        status=status or TestEnvironmentStatus.CONFIRMED,
        env_vars_json=json.dumps(ENV_VARS if env_vars is None else env_vars),
    )
    db_session.add(row)
    db_session.commit()
    return row


def _seed_plan(db_session, requirement, status=None):
    plan = TestPlan(
        requirement_id=requirement.id,
        status=status or TestPlanStatus.APPROVED,
        complexity="medium",
        summary="Plan",
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    case = TestCase(
        test_plan_id=plan.id,
        position=0,
        title="Export with one row",
        steps="Click export",
        expected_result="A CSV downloads",
        case_type="functional",
        priority=TestCasePriority.HIGH,
    )
    db_session.add(case)
    db_session.commit()
    return plan


def _ready_sprint(
    db_session, plan_status=None, env_status=None, env_vars=None, readme_user_provided=False
):
    """A sprint where exploration is allowed: confirmed req + plan + env."""
    sprint = _seed_sprint(db_session, readme_user_provided=readme_user_provided)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    _seed_plan(db_session, requirement, status=plan_status)
    _seed_environment(db_session, sprint, env_vars=env_vars, status=env_status)
    db_session.refresh(sprint)
    db_session.refresh(requirement)
    return sprint, requirement


@pytest.fixture
def stub_charters(monkeypatch):
    """Patch llm.generate_charters as the route module sees it."""
    from backend.routes import exploratory as routes

    state = {
        "result": llm.CharterResult(
            charters=[
                llm.CharterItem(charter="Explore export triggers", sfdipot_areas=["Function"]),
                llm.CharterItem(charter="Explore export edge data", sfdipot_areas=["Data"]),
            ],
            base_url_env_vars=["APP_URL"],
        ),
        "error": None,
        "calls": [],
    }

    def fake(**kwargs):
        state["calls"].append(kwargs)
        if state["error"] is not None:
            raise state["error"]
        return state["result"]

    monkeypatch.setattr(routes.llm, "generate_charters", fake)
    return state


@pytest.fixture
def stub_queue(monkeypatch):
    from backend.routes import exploratory as routes

    class _Stub:
        def __init__(self):
            self.enqueued: list[int] = []

        def enqueue_exploration(self, run_id):
            self.enqueued.append(run_id)
            from types import SimpleNamespace

            return SimpleNamespace(id=f"exploration-job-{run_id}")

    stub = _Stub()
    monkeypatch.setattr(routes, "get_queue_service", lambda: stub)
    return stub


@pytest.fixture(autouse=True)
def _isolate_refresh(monkeypatch):
    """Keep README/file-tree refresh deterministic: no network calls unless
    a test opts in via the ``refresh_stub`` fixture.

    Autouse because both the generate and create routes touch these — without
    it these tests run against the real resolvers and pass only because the
    refresh block is best-effort and the seeded sprints have nothing to fetch.
    """
    from backend.routes import exploratory as routes

    async def _noop_resolve_readme(*args, **kwargs):
        return None

    async def _noop_refresh_file_tree(*args, **kwargs):
        return None

    monkeypatch.setattr(routes, "resolve_readme", _noop_resolve_readme)
    monkeypatch.setattr(routes, "refresh_file_tree", _noop_refresh_file_tree)


@pytest.fixture
def refresh_stub(monkeypatch):
    """Recording refresh stub, reusing the scripted-run tests' helper.

    Same shape as `create_test_run`'s refresh block, so the stub is shared
    rather than duplicated; only the monkeypatch target differs.
    """
    from backend.routes import exploratory as routes

    stub = _RefreshStub()
    monkeypatch.setattr(routes, "resolve_readme", stub.resolve_readme)
    monkeypatch.setattr(routes, "refresh_file_tree", stub.refresh_file_tree)
    return stub


def _charter_payload(requirement_id, charters=None, url_vars=None):
    return {
        "requirement_id": requirement_id,
        "charters": charters
        if charters is not None
        else [{"charter": "Explore export", "sfdipot_areas": ["Function"]}],
        "base_url_env_vars": url_vars if url_vars is not None else ["APP_URL"],
    }


# ── charter generation ────────────────────────────────────────────────


class TestGenerateCharters:
    @pytest.mark.asyncio
    async def test_returns_charters_url_vars_and_projection(
        self, async_client, db_session, stub_charters
    ):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-charters/generate",
            json={"requirement_id": requirement.id},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert [c["charter"] for c in body["charters"]] == [
            "Explore export triggers",
            "Explore export edge data",
        ]
        assert body["base_url_env_vars"] == ["APP_URL"]
        assert body["charter_count"] == 2
        assert body["projected_minutes"] > 0
        assert body["requirement_name"] == requirement.name

    @pytest.mark.asyncio
    async def test_approved_cases_passed_as_already_covered(
        self, async_client, db_session, stub_charters
    ):
        sprint, requirement = _ready_sprint(db_session)

        await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-charters/generate",
            json={"requirement_id": requirement.id},
        )

        covered = stub_charters["calls"][-1]["covered_cases"]
        assert [c.title for c in covered] == ["Export with one row"]

    @pytest.mark.asyncio
    async def test_persists_nothing(self, async_client, db_session, stub_charters):
        from sqlmodel import select

        sprint, requirement = _ready_sprint(db_session)

        await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-charters/generate",
            json={"requirement_id": requirement.id},
        )

        assert db_session.exec(select(ExploratoryRun)).all() == []

    @pytest.mark.asyncio
    async def test_422_when_sprint_finished(self, async_client, db_session, stub_charters):
        sprint, requirement = _ready_sprint(db_session)
        sprint.active = False
        db_session.add(sprint)
        db_session.commit()

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-charters/generate",
            json={"requirement_id": requirement.id},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_422_when_requirement_not_confirmed(
        self, async_client, db_session, stub_charters
    ):
        sprint, _ = _ready_sprint(db_session)
        other = _seed_requirement(db_session, sprint, status=RequirementStatus.READY)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-charters/generate",
            json={"requirement_id": other.id},
        )
        assert resp.status_code == 422
        assert "not confirmed" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_when_plan_not_approved(self, async_client, db_session, stub_charters):
        sprint, requirement = _ready_sprint(db_session, plan_status=TestPlanStatus.DRAFT)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-charters/generate",
            json={"requirement_id": requirement.id},
        )
        assert resp.status_code == 422
        assert "approved test plan" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_when_environment_not_confirmed(
        self, async_client, db_session, stub_charters
    ):
        sprint, requirement = _ready_sprint(db_session, env_status=TestEnvironmentStatus.NEEDS_INFO)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-charters/generate",
            json={"requirement_id": requirement.id},
        )
        assert resp.status_code == 422
        assert "test environment must be confirmed" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_502_on_llm_error(self, async_client, db_session, stub_charters):
        sprint, requirement = _ready_sprint(db_session)
        stub_charters["error"] = llm.LLMError("provider exploded")

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-charters/generate",
            json={"requirement_id": requirement.id},
        )
        assert resp.status_code == 502
        assert "provider exploded" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_502_when_nominated_var_missing(self, async_client, db_session, stub_charters):
        sprint, requirement = _ready_sprint(db_session)
        stub_charters["result"] = llm.CharterResult(
            charters=[llm.CharterItem(charter="Explore", sfdipot_areas=["Function"])],
            base_url_env_vars=["NOT_A_REAL_VAR"],
        )

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-charters/generate",
            json={"requirement_id": requirement.id},
        )
        assert resp.status_code == 502
        assert "does not exist" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_502_when_nominated_var_is_not_a_url(
        self, async_client, db_session, stub_charters
    ):
        sprint, requirement = _ready_sprint(db_session)
        stub_charters["result"] = llm.CharterResult(
            charters=[llm.CharterItem(charter="Explore", sfdipot_areas=["Function"])],
            base_url_env_vars=["PW"],  # a password, not a URL
        )

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-charters/generate",
            json={"requirement_id": requirement.id},
        )
        assert resp.status_code == 502
        assert "http(s) URL" in resp.json()["detail"]


# ── run creation ──────────────────────────────────────────────────────


class TestCreateRun:
    @pytest.mark.asyncio
    async def test_creates_run_and_sessions_in_charter_order(
        self, async_client, db_session, stub_queue
    ):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(
                requirement.id,
                charters=[
                    {"charter": "first", "sfdipot_areas": ["Function"]},
                    {"charter": "second", "sfdipot_areas": ["Data", "Time"]},
                ],
            ),
        )

        assert resp.status_code == 201
        body = resp.json()
        assert [s["charter"] for s in body["sessions"]] == ["first", "second"]
        assert body["sessions"][1]["sfdipot_areas"] == ["Data", "Time"]
        assert body["status"] == ExploratoryRunStatus.PENDING
        assert stub_queue.enqueued == [body["id"]]

    @pytest.mark.asyncio
    async def test_persists_nominated_url_vars(self, async_client, db_session, stub_queue):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(requirement.id, url_vars=["APP_URL", "API_URL"]),
        )

        assert resp.status_code == 201
        assert resp.json()["base_url_env_vars"] == ["APP_URL", "API_URL"]

    @pytest.mark.asyncio
    async def test_refreshes_readme_and_file_tree_once(
        self, async_client, db_session, stub_queue, refresh_stub
    ):
        """Every charter shares one repo snapshot, like create_test_run does."""
        sprint, requirement = _ready_sprint(db_session, readme_user_provided=False)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(
                requirement.id,
                charters=[
                    {"charter": "Explore export", "sfdipot_areas": ["Function"]},
                    {"charter": "Explore edge data", "sfdipot_areas": ["Data"]},
                ],
            ),
        )

        assert resp.status_code == 201
        assert len(refresh_stub.readme_calls) == 1
        assert refresh_stub.readme_calls[0]["force_refresh"] is True
        assert len(refresh_stub.file_tree_calls) == 1

    @pytest.mark.asyncio
    async def test_skips_readme_refresh_when_user_provided(
        self, async_client, db_session, stub_queue, refresh_stub
    ):
        """A user-uploaded README is authoritative and never overwritten."""
        sprint, requirement = _ready_sprint(db_session, readme_user_provided=True)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(requirement.id),
        )

        assert resp.status_code == 201
        assert refresh_stub.readme_calls == []
        assert len(refresh_stub.file_tree_calls) == 1

    @pytest.mark.asyncio
    async def test_refresh_failure_does_not_block_run_creation(
        self, async_client, db_session, stub_queue, refresh_stub
    ):
        refresh_stub.raise_on_readme = True
        sprint, requirement = _ready_sprint(db_session, readme_user_provided=False)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(requirement.id),
        )

        assert resp.status_code == 201
        assert len(resp.json()["sessions"]) == 1

    @pytest.mark.asyncio
    async def test_422_on_duplicate_in_progress_run(self, async_client, db_session, stub_queue):
        sprint, requirement = _ready_sprint(db_session)
        _seed_exploratory_run(db_session, sprint, requirement, status=ExploratoryRunStatus.RUNNING)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(requirement.id),
        )
        assert resp.status_code == 422
        assert "already has an exploratory run in progress" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_allows_a_new_run_after_one_completed(self, async_client, db_session, stub_queue):
        sprint, requirement = _ready_sprint(db_session)
        _seed_exploratory_run(
            db_session, sprint, requirement, status=ExploratoryRunStatus.COMPLETED
        )

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(requirement.id),
        )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_422_on_zero_charters(self, async_client, db_session, stub_queue):
        sprint, requirement = _ready_sprint(db_session)
        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(requirement.id, charters=[]),
        )
        assert resp.status_code == 422
        assert "At least one charter" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_on_blank_charter(self, async_client, db_session, stub_queue):
        sprint, requirement = _ready_sprint(db_session)
        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(
                requirement.id, charters=[{"charter": "   ", "sfdipot_areas": []}]
            ),
        )
        assert resp.status_code == 422
        assert "cannot be blank" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_over_charter_cap(self, async_client, db_session, stub_queue, monkeypatch):
        from backend.routes import exploratory as routes

        monkeypatch.setattr(routes, "EXPLORATORY_MAX_CHARTERS", 2)
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(
                requirement.id,
                charters=[{"charter": f"c{i}", "sfdipot_areas": []} for i in range(3)],
            ),
        )
        assert resp.status_code == 422
        assert "At most 2 charters" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_on_unknown_sfdipot_area(self, async_client, db_session, stub_queue):
        sprint, requirement = _ready_sprint(db_session)
        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(
                requirement.id,
                charters=[{"charter": "Explore", "sfdipot_areas": ["Usability"]}],
            ),
        )
        assert resp.status_code == 422
        assert "Unknown SFDIPOT area" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_when_client_sends_a_bogus_url_var(
        self, async_client, db_session, stub_queue
    ):
        """The client is not trusted with what the generate call returned."""
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(requirement.id, url_vars=["MADE_UP"]),
        )
        assert resp.status_code == 422
        assert "does not exist" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_when_client_sends_a_non_url_var(self, async_client, db_session, stub_queue):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(requirement.id, url_vars=["PW"]),
        )
        assert resp.status_code == 422
        assert "http(s) URL" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_when_sprint_finished(self, async_client, db_session, stub_queue):
        sprint, requirement = _ready_sprint(db_session)
        sprint.active = False
        db_session.add(sprint)
        db_session.commit()

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(requirement.id),
        )
        assert resp.status_code == 422


# ── reads ─────────────────────────────────────────────────────────────


class TestReads:
    def _run_with_findings(self, db_session):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_exploratory_run(
            db_session, sprint, requirement, status=ExploratoryRunStatus.COMPLETED
        )
        session_row = _seed_exploratory_session(
            db_session, run, status=ExploratorySessionStatus.COMPLETED, actions_used=14
        )
        _seed_exploratory_finding(db_session, session_row, position=0)
        _seed_exploratory_finding(
            db_session,
            session_row,
            position=1,
            finding_type=FindingType.ISSUE,
            severity=FindingSeverity.LOW,
            title="No admin credentials",
        )
        return sprint, run, session_row

    @pytest.mark.asyncio
    async def test_list_returns_newest_first_with_counts(self, async_client, db_session):
        sprint, requirement = _ready_sprint(db_session)
        first = _seed_exploratory_run(db_session, sprint, requirement)
        second = _seed_exploratory_run(db_session, sprint, requirement)

        resp = await async_client.get(f"/api/sprints/{sprint.id}/exploratory-runs")

        assert resp.status_code == 200
        assert [row["id"] for row in resp.json()] == [second.id, first.id]

    @pytest.mark.asyncio
    async def test_finding_counts_aggregate_by_type_and_severity(self, async_client, db_session):
        sprint, run, _ = self._run_with_findings(db_session)

        resp = await async_client.get(f"/api/exploratory-runs/{run.id}")

        body = resp.json()
        assert body["bug_count"] == 1
        assert body["issue_count"] == 1
        assert body["high_severity_count"] == 1
        assert body["sessions"][0]["finding_count"] == 2

    @pytest.mark.asyncio
    async def test_session_sheet_includes_action_log_and_findings(self, async_client, db_session):
        _, _, session_row = self._run_with_findings(db_session)
        session_row.action_log = "snapshot() -> page"
        db_session.add(session_row)
        db_session.commit()

        resp = await async_client.get(f"/api/exploratory-sessions/{session_row.id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["action_log"] == "snapshot() -> page"
        assert body["actions_used"] == 14
        assert len(body["findings"]) == 2
        assert body["sfdipot_areas"] == ["Function", "Data"]

    @pytest.mark.asyncio
    async def test_finding_reports_screenshot_presence_not_path(self, async_client, db_session):
        _, _, session_row = self._run_with_findings(db_session)

        resp = await async_client.get(f"/api/exploratory-sessions/{session_row.id}")

        finding = resp.json()["findings"][0]
        assert finding["has_screenshot"] is False
        assert "screenshot_path" not in finding

    @pytest.mark.asyncio
    async def test_finding_reports_where_it_was_observed(self, async_client, db_session):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_exploratory_run(
            db_session, sprint, requirement, status=ExploratoryRunStatus.COMPLETED
        )
        session_row = _seed_exploratory_session(
            db_session, run, status=ExploratorySessionStatus.COMPLETED
        )
        _seed_exploratory_finding(
            db_session,
            session_row,
            position=0,
            environment="Chromium 131 · viewport 1280x720 · https://app.test/checkout",
        )
        # No environment: a finding recorded before capture existed.
        _seed_exploratory_finding(db_session, session_row, position=1, title="Older finding")

        resp = await async_client.get(f"/api/exploratory-sessions/{session_row.id}")

        findings = resp.json()["findings"]
        assert findings[0]["environment"] == (
            "Chromium 131 · viewport 1280x720 · https://app.test/checkout"
        )
        assert findings[1]["environment"] is None

    @pytest.mark.asyncio
    async def test_run_404(self, async_client, db_session):
        assert (await async_client.get("/api/exploratory-runs/999999")).status_code == 404

    @pytest.mark.asyncio
    async def test_session_404(self, async_client, db_session):
        assert (await async_client.get("/api/exploratory-sessions/999999")).status_code == 404

    @pytest.mark.asyncio
    async def test_screenshot_404_when_finding_has_none(self, async_client, db_session):
        _, _, session_row = self._run_with_findings(db_session)
        finding_id = session_row.findings[0].id

        resp = await async_client.get(f"/api/exploratory-findings/{finding_id}/screenshot")

        assert resp.status_code == 404
        assert "No screenshot available" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_screenshot_404_when_file_is_gone(self, async_client, db_session):
        _, _, session_row = self._run_with_findings(db_session)
        finding = session_row.findings[0]
        finding.screenshot_path = "/nonexistent/finding_0.png"
        db_session.add(finding)
        db_session.commit()

        resp = await async_client.get(f"/api/exploratory-findings/{finding.id}/screenshot")

        assert resp.status_code == 404
        assert "no longer available" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_screenshot_served_when_present(self, async_client, db_session, tmp_path):
        _, _, session_row = self._run_with_findings(db_session)
        png = tmp_path / "finding_0.png"
        png.write_bytes(b"PNGDATA")
        finding = session_row.findings[0]
        finding.screenshot_path = str(png)
        db_session.add(finding)
        db_session.commit()

        resp = await async_client.get(f"/api/exploratory-findings/{finding.id}/screenshot")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"


# ── restart and summarize ─────────────────────────────────────────────


class TestRestart:
    @pytest.mark.asyncio
    async def test_restarts_failed_run_and_reenqueues(self, async_client, db_session, stub_queue):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_exploratory_run(
            db_session,
            sprint,
            requirement,
            status=ExploratoryRunStatus.FAILED,
            error="worker died",
            retry_count=3,
        )

        resp = await async_client.post(f"/api/exploratory-runs/{run.id}/restart")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == ExploratoryRunStatus.PENDING
        assert body["error"] is None
        assert stub_queue.enqueued == [run.id]

    @pytest.mark.asyncio
    async def test_does_not_touch_session_rows(self, async_client, db_session, stub_queue):
        """Charter-level resumability lives in the task, not this route."""
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_exploratory_run(
            db_session, sprint, requirement, status=ExploratoryRunStatus.FAILED
        )
        done = _seed_exploratory_session(db_session, run, status=ExploratorySessionStatus.COMPLETED)

        await async_client.post(f"/api/exploratory-runs/{run.id}/restart")

        db_session.expire_all()
        assert (
            db_session.get(ExploratorySession, done.id).status == ExploratorySessionStatus.COMPLETED
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            ExploratoryRunStatus.PENDING,
            ExploratoryRunStatus.RUNNING,
            ExploratoryRunStatus.COMPLETED,
        ],
    )
    async def test_422_unless_failed(self, async_client, db_session, stub_queue, status):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_exploratory_run(db_session, sprint, requirement, status=status)

        resp = await async_client.post(f"/api/exploratory-runs/{run.id}/restart")

        assert resp.status_code == 422
        assert "Only failed" in resp.json()["detail"]


class TestSummarize:
    @pytest.fixture
    def stub_summary(self, monkeypatch):
        from backend.routes import exploratory as routes

        state = {"summary": "Export is broadly sound.", "error": None}

        def fake(**kwargs):
            if state["error"] is not None:
                raise state["error"]
            return llm.ExplorationSummaryResult(summary=state["summary"])

        monkeypatch.setattr(routes.llm, "summarize_exploration", fake)
        return state

    def _completed_run(self, db_session):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_exploratory_run(
            db_session, sprint, requirement, status=ExploratoryRunStatus.COMPLETED
        )
        _seed_exploratory_session(db_session, run, status=ExploratorySessionStatus.COMPLETED)
        return run

    @pytest.mark.asyncio
    async def test_fills_a_null_summary(self, async_client, db_session, stub_summary):
        run = self._completed_run(db_session)

        resp = await async_client.post(f"/api/exploratory-runs/{run.id}/summarize")

        assert resp.status_code == 200
        assert resp.json()["summary"] == "Export is broadly sound."
        db_session.expire_all()
        assert db_session.get(ExploratoryRun, run.id).summary == "Export is broadly sound."

    @pytest.mark.asyncio
    async def test_regenerates_an_existing_summary(self, async_client, db_session, stub_summary):
        run = self._completed_run(db_session)
        run.summary = "stale text"
        db_session.add(run)
        db_session.commit()
        stub_summary["summary"] = "fresh text"

        resp = await async_client.post(f"/api/exploratory-runs/{run.id}/summarize")

        assert resp.status_code == 200
        assert resp.json()["summary"] == "fresh text"

    @pytest.mark.asyncio
    async def test_502_leaves_existing_summary_untouched(
        self, async_client, db_session, stub_summary
    ):
        run = self._completed_run(db_session)
        run.summary = "previous summary"
        db_session.add(run)
        db_session.commit()
        stub_summary["error"] = llm.LLMError("provider down")

        resp = await async_client.post(f"/api/exploratory-runs/{run.id}/summarize")

        assert resp.status_code == 502
        db_session.expire_all()
        assert db_session.get(ExploratoryRun, run.id).summary == "previous summary"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            ExploratoryRunStatus.PENDING,
            ExploratoryRunStatus.RUNNING,
            ExploratoryRunStatus.FAILED,
        ],
    )
    async def test_422_unless_completed(self, async_client, db_session, stub_summary, status):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_exploratory_run(db_session, sprint, requirement, status=status)

        resp = await async_client.post(f"/api/exploratory-runs/{run.id}/summarize")

        assert resp.status_code == 422
        assert "Only completed" in resp.json()["detail"]


class TestExploratoryOutdated:
    """Same staleness vocabulary as scripted runs — one meaning across modes."""

    @pytest.mark.asyncio
    async def test_reasons_serialized_and_default_empty(self, async_client, db_session):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_exploratory_run(
            db_session,
            sprint,
            requirement,
            status=ExploratoryRunStatus.COMPLETED,
            requirement_revision=requirement.content_revision,
            plan_revision=requirement.test_plan.content_revision,
            env_revision=sprint.test_environment.content_revision,
        )

        resp = await async_client.get(f"/api/exploratory-runs/{run.id}")

        assert resp.status_code == 200
        assert resp.json()["outdated_reasons"] == []
        assert resp.json()["requirement_deleted"] is False

    @pytest.mark.asyncio
    async def test_plan_edit_marks_the_run_outdated(self, async_client, db_session):
        """Charter generation is shown the approved cases, so the plan counts."""
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_exploratory_run(
            db_session,
            sprint,
            requirement,
            status=ExploratoryRunStatus.COMPLETED,
            requirement_revision=requirement.content_revision,
            plan_revision=requirement.test_plan.content_revision,
            env_revision=sprint.test_environment.content_revision,
        )
        requirement.test_plan.content_revision += 1
        db_session.add(requirement.test_plan)
        db_session.commit()

        resp = await async_client.get(f"/api/exploratory-runs/{run.id}")

        assert resp.json()["outdated_reasons"] == ["test_plan"]

    @pytest.mark.asyncio
    async def test_outdated_run_cannot_restart(self, async_client, db_session):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_exploratory_run(
            db_session,
            sprint,
            requirement,
            status=ExploratoryRunStatus.FAILED,
            requirement_revision=requirement.content_revision,
            plan_revision=requirement.test_plan.content_revision,
            env_revision=sprint.test_environment.content_revision,
        )
        requirement.content_revision += 1
        db_session.add(requirement)
        db_session.commit()

        resp = await async_client.post(f"/api/exploratory-runs/{run.id}/restart")

        assert resp.status_code == 422
        assert "out of date" in resp.json()["detail"]


class TestExportFindingsToggle:
    """Mirrors the scripted route — one rule, worded identically."""

    def _connect_tracker(self, db_session, sprint):
        from backend.models.database import IssueTrackerConfig
        from backend.utils.crypto import encrypt_token

        db_session.add(
            IssueTrackerConfig(
                sprint_id=sprint.id,
                provider="github",
                target="acme/shop",
                api_token=encrypt_token("dummy-token"),
            )
        )
        db_session.commit()
        db_session.refresh(sprint)

    def _reload_run(self, db_session, run_id):
        db_session.expire_all()
        return db_session.get(ExploratoryRun, run_id)

    @pytest.mark.asyncio
    async def test_defaults_to_false(self, async_client, db_session, stub_queue):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json=_charter_payload(requirement.id),
        )

        assert resp.status_code == 201
        assert self._reload_run(db_session, resp.json()["id"]).export_findings is False

    @pytest.mark.asyncio
    async def test_422_when_on_with_no_tracker(self, async_client, db_session, stub_queue):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json={**_charter_payload(requirement.id), "export_findings": True},
        )

        assert resp.status_code == 422
        assert "Connect an issue tracker" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_persisted_on_the_run(self, async_client, db_session, stub_queue):
        sprint, requirement = _ready_sprint(db_session)
        self._connect_tracker(db_session, sprint)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/exploratory-runs",
            json={**_charter_payload(requirement.id), "export_findings": True},
        )

        assert resp.status_code == 201
        assert self._reload_run(db_session, resp.json()["id"]).export_findings is True
