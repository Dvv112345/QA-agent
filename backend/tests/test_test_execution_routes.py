"""Tests for backend/routes/test_execution.py — create runs, list, detail,
script download, restart.

Rows are seeded directly via ``db_session``; the queue is a recording stub.
"""

from types import SimpleNamespace

import pytest

from backend.models.database import (
    FindingSeverity,
    FindingType,
    RequirementStatus,
    TestCaseExecutionStatus,
    TestExecution,
    TestExecutionStatus,
    TestPlanStatus,
    TestRun,
)
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import (
    _seed_test_case,
    _seed_test_case_execution,
    _seed_test_env,
    _seed_test_execution,
    _seed_test_plan,
    _seed_test_run,
)


class _StubQueueService:
    """Records enqueued execution ids and returns fake jobs."""

    def __init__(self, available: bool = True):
        self.available = available
        self.enqueued_executions: list[int] = []

    def enqueue_test_execution(self, test_execution_id: int):
        if not self.available:
            return None
        self.enqueued_executions.append(test_execution_id)
        return SimpleNamespace(id=f"execution-job-{test_execution_id}")


@pytest.fixture
def stub_queue(monkeypatch):
    stub = _StubQueueService()
    import backend.routes.test_execution as test_execution_module

    monkeypatch.setattr(test_execution_module, "get_queue_service", lambda: stub)
    return stub


class _RefreshStub:
    """Records calls to the README/file-tree refresh functions.

    Defaults to a no-op success (returns ``None``, does nothing to
    ``sprint.repo``) so unrelated tests don't hit the network. Set
    ``.raise_on_readme`` / ``.raise_on_file_tree`` to exercise the
    best-effort failure path.
    """

    def __init__(self):
        self.readme_calls: list[dict] = []
        self.file_tree_calls: list = []
        self.raise_on_readme = False
        self.raise_on_file_tree = False

    async def resolve_readme(self, sprint, **kwargs):
        self.readme_calls.append({"sprint_id": sprint.id, **kwargs})
        if self.raise_on_readme:
            raise RuntimeError("boom")
        return "# Fresh README"

    async def refresh_file_tree(self, sprint):
        self.file_tree_calls.append(sprint.id)
        if self.raise_on_file_tree:
            raise RuntimeError("boom")
        return None


@pytest.fixture(autouse=True)
def _isolate_refresh(monkeypatch):
    """Keep README/file-tree refresh deterministic: no network calls unless
    a test opts in via the ``refresh_stub`` fixture."""
    from backend.utils import readme_utils

    async def _noop_resolve_readme(*args, **kwargs):
        return None

    async def _noop_refresh_file_tree(*args, **kwargs):
        return None

    monkeypatch.setattr(readme_utils, "resolve_readme", _noop_resolve_readme)
    monkeypatch.setattr(readme_utils, "refresh_file_tree", _noop_refresh_file_tree)


@pytest.fixture
def refresh_stub(monkeypatch):
    """Recording refresh stub.

    Patched on ``readme_utils`` rather than the route module: the route
    calls ``refresh_project_context``, which reaches these two as module
    globals — so this stubs the network while still exercising the real
    composition (including the user-provided-README rule).
    """
    from backend.utils import readme_utils

    stub = _RefreshStub()
    monkeypatch.setattr(readme_utils, "resolve_readme", stub.resolve_readme)
    monkeypatch.setattr(readme_utils, "refresh_file_tree", stub.refresh_file_tree)
    return stub


def _seed_runnable_requirement(db_session, sprint, name="Login", case_count=2):
    """A confirmed requirement with an approved plan and cases, ready to run."""
    requirement = _seed_requirement(
        db_session, sprint, status=RequirementStatus.CONFIRMED, name=name
    )
    plan = _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED)
    for i in range(case_count):
        _seed_test_case(db_session, plan, position=i, title=f"{name} case {i}")
    db_session.refresh(requirement)
    return requirement


def _reload_run(db_session, run_id) -> TestRun:
    db_session.expire_all()
    return db_session.get(TestRun, run_id)


def _reload_execution(db_session, execution_id) -> TestExecution:
    db_session.expire_all()
    return db_session.get(TestExecution, execution_id)


# ── POST /api/sprints/{id}/test-runs ──────────────────────────────────


def _seed_run_ready_sprint(db_session, active=True, readme_user_provided=False):
    """A sprint whose test environment is confirmed — a precondition for
    creating a run, mirroring the exploratory route."""
    from backend.models.database import TestEnvironmentStatus

    sprint = _seed_sprint(db_session, active=active, readme_user_provided=readme_user_provided)
    _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)
    db_session.refresh(sprint)
    return sprint


class TestCreateTestRun:
    @pytest.mark.asyncio
    async def test_404_unknown_sprint(self, async_client, stub_queue):
        resp = await async_client.post(
            "/api/sprints/99999/test-runs", json={"requirement_ids": [1]}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_422_finished_sprint(self, async_client, db_session, stub_queue):
        sprint = _seed_run_ready_sprint(db_session, active=False)
        requirement = _seed_runnable_requirement(db_session, sprint)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_422_when_environment_not_confirmed(self, async_client, db_session, stub_queue):
        """Scripted runs need a confirmed environment as much as exploratory
        ones do — the worker injects its variables into every subprocess.

        Newly reachable: adding a requirement un-confirms the environment
        without removing plans, so an approved plan can outlive confirmation.
        Without this the run is created and every case fails on missing
        variables.
        """
        from backend.models.database import TestEnvironmentStatus

        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.READY)
        requirement = _seed_runnable_requirement(db_session, sprint)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )

        assert resp.status_code == 422
        assert "Confirm the test environment" in resp.json()["detail"]
        assert stub_queue.enqueued_executions == []

    @pytest.mark.asyncio
    async def test_422_when_environment_has_no_variables(
        self, async_client, db_session, stub_queue
    ):
        from backend.models.database import TestEnvironmentStatus

        sprint = _seed_sprint(db_session)
        _seed_test_env(
            db_session, sprint, status=TestEnvironmentStatus.CONFIRMED, env_vars_json=None
        )
        requirement = _seed_runnable_requirement(db_session, sprint)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )

        assert resp.status_code == 422
        assert "variables" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_empty_requirement_ids(self, async_client, db_session, stub_queue):
        sprint = _seed_run_ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs", json={"requirement_ids": []}
        )
        assert resp.status_code == 422
        assert "at least one" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_422_foreign_or_unconfirmed_requirement(
        self, async_client, db_session, stub_queue
    ):
        sprint = _seed_run_ready_sprint(db_session)
        not_confirmed = _seed_requirement(db_session, sprint, status=RequirementStatus.READY)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [not_confirmed.id, 99999]},
        )
        assert resp.status_code == 422
        assert str(not_confirmed.id) in resp.json()["detail"]
        assert "99999" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_plan_not_approved(self, async_client, db_session, stub_queue):
        sprint = _seed_run_ready_sprint(db_session)
        requirement = _seed_requirement(
            db_session, sprint, status=RequirementStatus.CONFIRMED, name="No Plan"
        )

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )
        assert resp.status_code == 422
        assert "No Plan" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_plan_draft_not_approved(self, async_client, db_session, stub_queue):
        sprint = _seed_run_ready_sprint(db_session)
        requirement = _seed_requirement(
            db_session, sprint, status=RequirementStatus.CONFIRMED, name="Draft Plan"
        )
        _seed_test_plan(db_session, requirement, status=TestPlanStatus.DRAFT)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )
        assert resp.status_code == 422
        assert "Draft Plan" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_already_in_progress(self, async_client, db_session, stub_queue):
        sprint = _seed_run_ready_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint)
        run = _seed_test_run(db_session, sprint)
        _seed_test_execution(db_session, run, requirement, status=TestExecutionStatus.RUNNING)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )
        assert resp.status_code == 422
        assert requirement.name in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_creates_run_with_executions_and_cases_in_position_order(
        self, async_client, db_session, stub_queue
    ):
        sprint = _seed_run_ready_sprint(db_session)
        login = _seed_runnable_requirement(db_session, sprint, name="Login", case_count=2)
        search = _seed_runnable_requirement(db_session, sprint, name="Search", case_count=1)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [login.id, search.id]},
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "running"
        assert len(data["executions"]) == 2
        assert data["executions"][0]["requirement_name"] == "Login"
        assert [c["test_case"]["title"] for c in data["executions"][0]["cases"]] == [
            "Login case 0",
            "Login case 1",
        ]
        assert data["executions"][1]["requirement_name"] == "Search"
        assert len(data["executions"][1]["cases"]) == 1
        assert all(c["status"] == "pending" for c in data["executions"][0]["cases"])

    @pytest.mark.asyncio
    async def test_enqueues_and_persists_job_id(self, async_client, db_session, stub_queue):
        sprint = _seed_run_ready_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )

        assert resp.status_code == 201
        run_id = resp.json()["id"]
        execution_id = resp.json()["executions"][0]["id"]
        assert stub_queue.enqueued_executions == [execution_id]
        row = _reload_execution(db_session, execution_id)
        assert row.job_id == f"execution-job-{execution_id}"
        assert _reload_run(db_session, run_id) is not None

    @pytest.mark.asyncio
    async def test_redis_down_leaves_rows_pending_without_job_id(
        self, async_client, db_session, stub_queue
    ):
        stub_queue.available = False
        sprint = _seed_run_ready_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )

        assert resp.status_code == 201
        execution_id = resp.json()["executions"][0]["id"]
        row = _reload_execution(db_session, execution_id)
        assert row.status == TestExecutionStatus.PENDING
        assert row.job_id is None

    @pytest.mark.asyncio
    async def test_refreshes_readme_and_file_tree_once_for_multiple_requirements(
        self, async_client, db_session, stub_queue, refresh_stub
    ):
        sprint = _seed_run_ready_sprint(db_session, readme_user_provided=False)
        login = _seed_runnable_requirement(db_session, sprint, name="Login", case_count=1)
        search = _seed_runnable_requirement(db_session, sprint, name="Search", case_count=1)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [login.id, search.id]},
        )

        assert resp.status_code == 201
        assert len(refresh_stub.readme_calls) == 1
        assert refresh_stub.readme_calls[0]["force_refresh"] is True
        assert len(refresh_stub.file_tree_calls) == 1

    @pytest.mark.asyncio
    async def test_skips_readme_refresh_when_user_provided(
        self, async_client, db_session, stub_queue, refresh_stub
    ):
        sprint = _seed_run_ready_sprint(db_session, readme_user_provided=True)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )

        assert resp.status_code == 201
        assert refresh_stub.readme_calls == []
        assert len(refresh_stub.file_tree_calls) == 1

    @pytest.mark.asyncio
    async def test_refresh_failure_does_not_block_run_creation(
        self, async_client, db_session, stub_queue, refresh_stub
    ):
        refresh_stub.raise_on_readme = True
        sprint = _seed_run_ready_sprint(db_session, readme_user_provided=False)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )

        assert resp.status_code == 201
        assert len(resp.json()["executions"]) == 1


# ── GET /api/sprints/{id}/test-runs ───────────────────────────────────


class TestListTestRuns:
    @pytest.mark.asyncio
    async def test_404_unknown_sprint(self, async_client):
        resp = await async_client.get("/api/sprints/99999/test-runs")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_newest_first(self, async_client, db_session):
        from datetime import datetime, timedelta, timezone

        sprint = _seed_sprint(db_session)
        older = _seed_test_run(
            db_session, sprint, created_at=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        newer = _seed_test_run(db_session, sprint, created_at=datetime.now(timezone.utc))

        resp = await async_client.get(f"/api/sprints/{sprint.id}/test-runs")

        assert resp.status_code == 200
        ids = [row["id"] for row in resp.json()]
        assert ids == [newer.id, older.id]

    @pytest.mark.asyncio
    async def test_counts_across_mixed_case_statuses(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=3)
        plan = requirement.test_plan
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(
            db_session, run, requirement, status=TestExecutionStatus.COMPLETED
        )
        statuses = [
            TestCaseExecutionStatus.PASSED,
            TestCaseExecutionStatus.FAILED,
            TestCaseExecutionStatus.ERROR,
        ]
        for case, status in zip(plan.cases, statuses, strict=True):
            _seed_test_case_execution(db_session, execution, case, status=status)

        resp = await async_client.get(f"/api/sprints/{sprint.id}/test-runs")

        assert resp.status_code == 200
        row = resp.json()[0]
        assert row["total_cases"] == 3
        assert row["passed_cases"] == 1
        assert row["failed_cases"] == 1
        assert row["error_cases"] == 1
        assert row["status"] == "completed"
        assert row["requirement_names"] == [requirement.name]


# ── GET /api/test-runs/{id} ───────────────────────────────────────────


class TestGetTestRun:
    @pytest.mark.asyncio
    async def test_404_unknown_run(self, async_client):
        resp = await async_client.get("/api/test-runs/99999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_nested_structure(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        plan = requirement.test_plan
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(db_session, run, requirement)
        _seed_test_case_execution(db_session, execution, plan.cases[0])

        resp = await async_client.get(f"/api/test-runs/{run.id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == run.id
        assert data["sprint_id"] == sprint.id
        assert len(data["executions"]) == 1
        assert data["executions"][0]["requirement_id"] == requirement.id
        assert len(data["executions"][0]["cases"]) == 1
        assert data["executions"][0]["cases"][0]["test_case"]["title"] == plan.cases[0].title

    def _run_with_case(self, db_session, **case_kwargs):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        plan = requirement.test_plan
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(db_session, run, requirement)
        case_exec = _seed_test_case_execution(db_session, execution, plan.cases[0])
        for field, value in case_kwargs.items():
            setattr(case_exec, field, value)
        db_session.add(case_exec)
        db_session.commit()
        return run

    @pytest.mark.asyncio
    async def test_failed_case_nests_a_bug_finding(self, async_client, db_session):
        run = self._run_with_case(
            db_session,
            status=TestCaseExecutionStatus.FAILED,
            finding_severity=FindingSeverity.HIGH,
            finding_title="Valid credentials are rejected",
            finding_steps_to_reproduce="Open /login\nSubmit valid credentials",
            finding_expected="The user reaches the dashboard",
            finding_actual="A 401 is returned",
            environment="Windows-10 · Python 3.12.4",
        )

        resp = await async_client.get(f"/api/test-runs/{run.id}")

        finding = resp.json()["executions"][0]["cases"][0]["finding"]
        assert finding["finding_type"] == FindingType.BUG
        assert finding["severity"] == FindingSeverity.HIGH
        assert finding["title"] == "Valid credentials are rejected"
        assert finding["steps_to_reproduce"] == "Open /login\nSubmit valid credentials"
        assert finding["expected"] == "The user reaches the dashboard"
        assert finding["actual"] == "A 401 is returned"
        assert finding["environment"] == "Windows-10 · Python 3.12.4"

    @pytest.mark.asyncio
    async def test_errored_case_nests_an_issue_finding(self, async_client, db_session):
        run = self._run_with_case(
            db_session,
            status=TestCaseExecutionStatus.ERROR,
            finding_severity=FindingSeverity.MEDIUM,
            finding_title="Could not verify: Valid login",
            finding_steps_to_reproduce="Open the login page",
            finding_expected="User lands on the dashboard.",
            finding_actual="The script was never made to run.",
        )

        resp = await async_client.get(f"/api/test-runs/{run.id}")

        finding = resp.json()["executions"][0]["cases"][0]["finding"]
        assert finding["finding_type"] == FindingType.ISSUE
        assert finding["environment"] is None

    @pytest.mark.asyncio
    async def test_passing_case_nests_no_finding(self, async_client, db_session):
        run = self._run_with_case(db_session, status=TestCaseExecutionStatus.PASSED)

        resp = await async_client.get(f"/api/test-runs/{run.id}")

        assert resp.json()["executions"][0]["cases"][0]["finding"] is None

    @pytest.mark.asyncio
    async def test_legacy_failed_case_nests_no_finding(self, async_client, db_session):
        """Rows written before findings were structured have no title, and
        must not surface as an all-null card."""
        run = self._run_with_case(
            db_session, status=TestCaseExecutionStatus.FAILED, error="something broke"
        )

        resp = await async_client.get(f"/api/test-runs/{run.id}")

        case = resp.json()["executions"][0]["cases"][0]
        assert case["finding"] is None
        assert case["error"] == "something broke"  # raw output still reported


# ── GET /api/test-case-executions/{id}/script ─────────────────────────


class TestDownloadScript:
    @pytest.mark.asyncio
    async def test_200_with_content_and_headers(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        plan = requirement.test_plan
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(db_session, run, requirement)
        case_execution = _seed_test_case_execution(
            db_session,
            execution,
            plan.cases[0],
            status=TestCaseExecutionStatus.PASSED,
            script_snapshot="print('hello')\n",
        )

        resp = await async_client.get(f"/api/test-case-executions/{case_execution.id}/script")

        assert resp.status_code == 200
        assert resp.text == "print('hello')\n"
        assert "attachment" in resp.headers["content-disposition"]
        assert f"test_case_{case_execution.id}.py" in resp.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_404_when_not_yet_run(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        plan = requirement.test_plan
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(db_session, run, requirement)
        case_execution = _seed_test_case_execution(db_session, execution, plan.cases[0])

        resp = await async_client.get(f"/api/test-case-executions/{case_execution.id}/script")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_404_for_missing_row(self, async_client):
        resp = await async_client.get("/api/test-case-executions/99999/script")
        assert resp.status_code == 404


# ── POST /api/test-executions/{id}/restart ────────────────────────────


class TestRestartTestExecution:
    @pytest.mark.asyncio
    async def test_404_for_missing_row(self, async_client):
        resp = await async_client.post("/api/test-executions/99999/restart")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_only_from_failed(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(
            db_session, run, requirement, status=TestExecutionStatus.COMPLETED
        )

        resp = await async_client.post(f"/api/test-executions/{execution.id}/restart")

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_clears_error_and_retries_then_enqueues(
        self, async_client, db_session, stub_queue
    ):
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(
            db_session,
            run,
            requirement,
            status=TestExecutionStatus.FAILED,
            error="boom",
            retry_count=3,
        )

        resp = await async_client.post(f"/api/test-executions/{execution.id}/restart")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["error"] is None
        assert stub_queue.enqueued_executions == [execution.id]
        row = _reload_execution(db_session, execution.id)
        assert row.retry_count == 0
        assert row.job_id == f"execution-job-{execution.id}"

    @pytest.mark.asyncio
    async def test_422_on_finished_sprint(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session, active=False)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(
            db_session, run, requirement, status=TestExecutionStatus.FAILED
        )

        resp = await async_client.post(f"/api/test-executions/{execution.id}/restart")

        assert resp.status_code == 422


# ── Outdated runs ──────────────────────────────────────────────────────


def _seed_stamped_execution(db_session, sprint, requirement, status=None):
    """A run + execution stamped exactly as the create route would stamp it."""
    run = _seed_test_run(db_session, sprint)
    execution = _seed_test_execution(
        db_session,
        run,
        requirement,
        status=status or TestExecutionStatus.FAILED,
        requirement_revision=requirement.content_revision,
        plan_revision=requirement.test_plan.content_revision,
        env_revision=(sprint.test_environment.content_revision if sprint.test_environment else 0),
    )
    return run, execution


class TestOutdatedRuns:
    """A run records the revisions it executed against; a later upstream edit
    is what makes it read as outdated."""

    def test_fresh_run_is_current(self, db_session):
        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint)
        requirement = _seed_runnable_requirement(db_session, sprint)
        _, execution = _seed_stamped_execution(db_session, sprint, requirement)

        assert execution.outdated_reasons == []
        assert execution.outdated is False
        assert execution.requirement_deleted is False

    @pytest.mark.parametrize("artifact", ["requirement", "test_plan", "test_environment"])
    def test_each_edit_is_attributed_alone(self, db_session, artifact):
        sprint = _seed_sprint(db_session)
        test_env = _seed_test_env(db_session, sprint)
        requirement = _seed_runnable_requirement(db_session, sprint)
        _, execution = _seed_stamped_execution(db_session, sprint, requirement)

        target = {
            "requirement": requirement,
            "test_plan": requirement.test_plan,
            "test_environment": test_env,
        }[artifact]
        target.content_revision += 1
        db_session.add(target)
        db_session.commit()

        assert execution.outdated_reasons == [artifact]

    def test_removing_the_plan_counts_as_outdated(self, db_session):
        """Not only an edit — a plan removed by a cascade is changed content too."""
        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint)
        requirement = _seed_runnable_requirement(db_session, sprint)
        _, execution = _seed_stamped_execution(db_session, sprint, requirement)

        db_session.delete(requirement.test_plan)
        db_session.commit()
        db_session.refresh(requirement)

        assert execution.outdated_reasons == ["test_plan"]

    def test_archived_requirement_reports_requirement_not_plan(self, db_session):
        """The plan went with the requirement, so reporting it too is noise."""
        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint)
        requirement = _seed_runnable_requirement(db_session, sprint)
        _, execution = _seed_stamped_execution(db_session, sprint, requirement)

        requirement.archived = True
        db_session.add(requirement)
        db_session.commit()

        assert execution.outdated_reasons == ["requirement"]
        assert execution.requirement_deleted is True
        assert execution.outdated is True

    def test_archived_requirement_plus_environment_edit_reports_both(self, db_session):
        """The environment comparison survives the deletion — independent facts."""
        sprint = _seed_sprint(db_session)
        test_env = _seed_test_env(db_session, sprint)
        requirement = _seed_runnable_requirement(db_session, sprint)
        _, execution = _seed_stamped_execution(db_session, sprint, requirement)

        requirement.archived = True
        test_env.content_revision += 1
        db_session.add_all([requirement, test_env])
        db_session.commit()

        assert execution.outdated_reasons == ["requirement", "test_environment"]

    def test_missing_environment_row_is_not_a_reason(self, db_session):
        """Absent is evidence of nothing — unlike a plan, it cannot be deleted."""
        sprint = _seed_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint)
        _, execution = _seed_stamped_execution(db_session, sprint, requirement)

        assert execution.outdated_reasons == []

    def test_run_unions_reasons_without_duplicates(self, db_session):
        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint)
        first = _seed_runnable_requirement(db_session, sprint, name="Login")
        second = _seed_runnable_requirement(db_session, sprint, name="Search")
        run = _seed_test_run(db_session, sprint)
        for requirement in (first, second):
            _seed_test_execution(
                db_session,
                run,
                requirement,
                status=TestExecutionStatus.COMPLETED,
                requirement_revision=requirement.content_revision,
                plan_revision=requirement.test_plan.content_revision,
            )
        first.content_revision += 1
        second.content_revision += 1
        db_session.add_all([first, second])
        db_session.commit()
        db_session.refresh(run)

        assert run.outdated_reasons == ["requirement"]  # unioned, not repeated
        assert run.outdated is True

    def test_preexisting_rows_read_as_current(self, db_session):
        """Every revision defaults to 0 on both sides — hence no backfill."""
        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint)
        requirement = _seed_runnable_requirement(db_session, sprint)
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(
            db_session, run, requirement, status=TestExecutionStatus.FAILED
        )

        assert execution.outdated_reasons == []


class TestRestartRefusedWhenOutdated:
    @pytest.mark.asyncio
    async def test_outdated_execution_cannot_restart(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint)
        requirement = _seed_runnable_requirement(db_session, sprint)
        _, execution = _seed_stamped_execution(db_session, sprint, requirement)
        requirement.content_revision += 1
        db_session.add(requirement)
        db_session.commit()

        resp = await async_client.post(f"/api/test-executions/{execution.id}/restart")

        assert resp.status_code == 422
        assert "out of date" in resp.json()["detail"]
        assert "the requirement changed" in resp.json()["detail"]
        assert stub_queue.enqueued_executions == []

    @pytest.mark.asyncio
    async def test_deleted_requirement_says_deleted(self, async_client, db_session, stub_queue):
        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint)
        requirement = _seed_runnable_requirement(db_session, sprint)
        _, execution = _seed_stamped_execution(db_session, sprint, requirement)
        requirement.archived = True
        db_session.add(requirement)
        db_session.commit()

        resp = await async_client.post(f"/api/test-executions/{execution.id}/restart")

        assert resp.status_code == 422
        assert "the requirement was deleted" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_reasons_serialized_on_the_list_endpoint(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        _seed_test_env(db_session, sprint)
        requirement = _seed_runnable_requirement(db_session, sprint)
        _seed_stamped_execution(
            db_session, sprint, requirement, status=TestExecutionStatus.COMPLETED
        )
        requirement.content_revision += 1
        db_session.add(requirement)
        db_session.commit()

        resp = await async_client.get(f"/api/sprints/{sprint.id}/test-runs")

        assert resp.status_code == 200
        assert resp.json()[0]["outdated_reasons"] == ["requirement"]
        assert resp.json()[0]["requirement_deleted"] is False


class TestListEndpointDoesNotScaleWithRequirements:
    """`outdated_reasons` walks relationships the list query must eager-load.

    This endpoint polls every 2.5s, and the traversal is invisible from the
    route — it happens inside a model property — so a lazy load here is easy
    to reintroduce and hard to notice. Pinned by counting statements rather
    than by inspecting the query, since that is the thing that actually
    matters.
    """

    @pytest.mark.asyncio
    async def test_query_count_is_flat_in_requirement_count(self, async_client, db_session):
        from sqlalchemy import event

        counts = {}
        for requirement_count in (2, 6):
            sprint = _seed_sprint(db_session)
            _seed_test_env(db_session, sprint)
            requirements = [
                _seed_runnable_requirement(db_session, sprint, name=f"R{i}", case_count=1)
                for i in range(requirement_count)
            ]
            run = _seed_test_run(db_session, sprint)
            for requirement in requirements:
                _seed_test_execution(
                    db_session,
                    run,
                    requirement,
                    status=TestExecutionStatus.COMPLETED,
                    requirement_revision=requirement.content_revision,
                    plan_revision=requirement.test_plan.content_revision,
                    env_revision=sprint.test_environment.content_revision,
                )
            db_session.commit()
            db_session.expire_all()

            counter = {"n": 0}
            engine = db_session.get_bind()

            @event.listens_for(engine, "before_cursor_execute")
            def _count(conn, cursor, statement, params, context, executemany, _c=counter):
                _c["n"] += 1

            resp = await async_client.get(f"/api/sprints/{sprint.id}/test-runs")
            event.remove(engine, "before_cursor_execute", _count)

            assert resp.status_code == 200
            counts[requirement_count] = counter["n"]

        # Eager-loaded, so tripling the requirements must not add queries.
        assert counts[2] == counts[6], (
            f"query count grew with requirement count: {counts} — "
            "something outdated_reasons touches is lazy-loading again"
        )


# ── Auth spot-check ────────────────────────────────────────────────────


class TestAuth:
    @pytest.mark.asyncio
    async def test_401_without_cookie(self, monkeypatch, db_session):
        from backend.tests.test_auth_routes import _make_client

        async with _make_client(monkeypatch, db_session) as client:
            resp = await client.get("/api/sprints/1/test-runs")
        assert resp.status_code == 401


class TestExportFindingsToggle:
    """The run carries the decision, so it is made once at run start."""

    def _connect_tracker(self, db_session, sprint):
        from backend.models.database import IssueTrackerConfig
        from backend.utils.crypto import encrypt_token

        db_session.add(
            IssueTrackerConfig(
                sprint_id=sprint.id,
                provider="jira",
                target="QA",
                api_token=encrypt_token("dummy-token"),
                base_url="https://acme.atlassian.net",
                account_email="qa@acme.test",
                issue_type="Bug",
            )
        )
        db_session.commit()
        db_session.refresh(sprint)

    @pytest.mark.asyncio
    async def test_defaults_to_false(self, async_client, db_session, stub_queue):
        sprint = _seed_run_ready_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id]},
        )

        assert resp.status_code == 201
        assert _reload_run(db_session, resp.json()["id"]).export_findings is False

    @pytest.mark.asyncio
    async def test_422_when_on_with_no_tracker(self, async_client, db_session, stub_queue):
        sprint = _seed_run_ready_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id], "export_findings": True},
        )

        assert resp.status_code == 422
        assert "Connect an issue tracker" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_persisted_on_the_run(self, async_client, db_session, stub_queue):
        sprint = _seed_run_ready_sprint(db_session)
        self._connect_tracker(db_session, sprint)
        requirement = _seed_runnable_requirement(db_session, sprint)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-runs",
            json={"requirement_ids": [requirement.id], "export_findings": True},
        )

        assert resp.status_code == 201
        assert _reload_run(db_session, resp.json()["id"]).export_findings is True


class TestExportFindingsEndpoint:
    """The manual half of the export rule — and not a fallback.

    A run that ended any way other than `completed` reaches its page with
    the bugs it did find unfiled by design, and this is how they get
    filed.
    """

    @pytest.fixture
    def export_spy(self, monkeypatch):
        from backend.services import finding_export

        calls: list = []

        def _spy(session, parent, *, requested=False):
            # The flag is recorded, not just accepted: this route passing
            # `requested=True` is what makes the button work on a run
            # whose start-time toggle was off.
            calls.append((parent.id, requested))
            return finding_export.ExportOutcome()

        monkeypatch.setattr(finding_export, "export_findings", _spy)
        return calls

    def _connect_tracker(self, db_session, sprint):
        from backend.models.database import IssueTrackerConfig
        from backend.utils.crypto import encrypt_token

        db_session.add(
            IssueTrackerConfig(
                sprint_id=sprint.id,
                provider="jira",
                target="QA",
                api_token=encrypt_token("dummy-token"),
                base_url="https://acme.atlassian.net",
                account_email="qa@acme.test",
                issue_type="Bug",
            )
        )
        db_session.commit()
        db_session.refresh(sprint)

    def _seed_failed_run(self, db_session, sprint, status="failed", export_findings=True):
        """A run whose execution ended without reaching the export path."""
        from backend.models.database import TestCaseExecutionStatus

        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        run = _seed_test_run(db_session, sprint, export_findings=export_findings)
        execution = _seed_test_execution(db_session, run, requirement, status=status)
        _seed_test_case_execution(
            db_session,
            execution,
            requirement.test_plan.cases[0],
            status=TestCaseExecutionStatus.FAILED,
            finding_severity="high",
            finding_title="Checkout returns 500",
            finding_steps_to_reproduce="Open /checkout",
            finding_expected="The order is created",
            finding_actual="HTTP 500",
        )
        db_session.refresh(run)
        return run

    @pytest.mark.asyncio
    async def test_404_unknown_run(self, async_client):
        resp = await async_client.post("/api/test-runs/99999/export-findings")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_422_without_a_tracker(self, async_client, db_session, export_spy):
        sprint = _seed_run_ready_sprint(db_session)
        run = self._seed_failed_run(db_session, sprint)

        resp = await async_client.post(f"/api/test-runs/{run.id}/export-findings")

        assert resp.status_code == 422
        assert export_spy == []

    @pytest.mark.asyncio
    async def test_files_the_findings_of_a_failed_run(self, async_client, db_session, export_spy):
        """`failed` is the path that deliberately skips automatic export —
        a superseded run, or one a finished sprint swept."""
        sprint = _seed_run_ready_sprint(db_session)
        self._connect_tracker(db_session, sprint)
        run = self._seed_failed_run(db_session, sprint)
        execution_id = run.executions[0].id

        resp = await async_client.post(f"/api/test-runs/{run.id}/export-findings")

        assert resp.status_code == 200
        assert export_spy == [(execution_id, True)]

    @pytest.mark.asyncio
    async def test_files_a_run_whose_toggle_was_off(self, async_client, db_session):
        """The whole recovery path for "I connected the tracker after
        starting the run".

        Deliberately drives the **real** exporter with only the transport
        stubbed: spying on `finding_export.export_findings` is exactly
        what let this ship inert, because the fast exit being spied over
        is the bug.
        """
        from backend.services import issue_tracker
        from backend.services.issue_tracker import IssueRef

        created: list = []

        def _create(config, report, context):
            created.append(report.title)
            return IssueRef(key="QA-9", url="https://acme.atlassian.net/browse/QA-9")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(issue_tracker, "create_issue", _create)
            sprint = _seed_run_ready_sprint(db_session)
            self._connect_tracker(db_session, sprint)
            run = self._seed_failed_run(db_session, sprint, export_findings=False)

            before = (await async_client.get(f"/api/test-runs/{run.id}")).json()
            assert before["unexported_finding_count"] == 1

            resp = await async_client.post(f"/api/test-runs/{run.id}/export-findings")

        assert resp.status_code == 200
        assert created == ["Checkout returns 500"]
        assert resp.json()["unexported_finding_count"] == 0
        assert resp.json()["exported_finding_count"] == 1

    @pytest.mark.asyncio
    async def test_a_run_with_nothing_pending_still_returns_the_detail(
        self, async_client, db_session
    ):
        """The button must never be a trap: a no-op is a 200, not an error."""
        sprint = _seed_run_ready_sprint(db_session)
        self._connect_tracker(db_session, sprint)
        run = _seed_test_run(db_session, sprint)

        resp = await async_client.post(f"/api/test-runs/{run.id}/export-findings")

        assert resp.status_code == 200
        assert resp.json()["id"] == run.id
        assert resp.json()["unexported_finding_count"] == 0

    @pytest.mark.asyncio
    async def test_succeeds_after_a_tracker_is_reconnected(
        self, async_client, db_session, export_spy
    ):
        sprint = _seed_run_ready_sprint(db_session)
        run = self._seed_failed_run(db_session, sprint)
        execution_id = run.executions[0].id
        first = await async_client.post(f"/api/test-runs/{run.id}/export-findings")
        assert first.status_code == 422

        self._connect_tracker(db_session, sprint)
        resp = await async_client.post(f"/api/test-runs/{run.id}/export-findings")

        assert resp.status_code == 200
        assert export_spy == [(execution_id, True)]


class TestExportRollup:
    """Counts and groups computed at response time, never stored."""

    def _seed_run_with_findings(self, db_session, sprint, receipts):
        """One case per entry in *receipts*, each a dict of tracker fields."""
        from backend.models.database import TestCaseExecutionStatus

        requirement = _seed_runnable_requirement(db_session, sprint, case_count=len(receipts))
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(db_session, run, requirement)
        for case, receipt in zip(requirement.test_plan.cases, receipts, strict=True):
            _seed_test_case_execution(
                db_session,
                execution,
                case,
                status=TestCaseExecutionStatus.FAILED,
                finding_severity="high",
                finding_title="Checkout returns 500",
                finding_steps_to_reproduce="Open /checkout",
                finding_expected="The order is created",
                finding_actual="HTTP 500",
                **receipt,
            )
        db_session.refresh(run)
        return run

    @pytest.mark.asyncio
    async def test_groups_findings_by_issue_key(self, async_client, db_session):
        """Six findings can be two tickets — the mapping has to be readable
        without opening every card."""
        sprint = _seed_run_ready_sprint(db_session)
        run = self._seed_run_with_findings(
            db_session,
            sprint,
            [
                {"tracker_issue_key": "QA-1", "tracker_issue_url": "https://x/QA-1"},
                {"tracker_issue_key": "QA-1", "tracker_issue_url": "https://x/QA-1"},
                {"tracker_issue_key": "QA-2", "tracker_issue_url": "https://x/QA-2"},
            ],
        )

        body = (await async_client.get(f"/api/test-runs/{run.id}")).json()

        assert body["exported_finding_count"] == 3
        assert body["exported_issue_count"] == 2
        assert body["unexported_finding_count"] == 0
        counts = {g["issue_key"]: g["finding_count"] for g in body["export_groups"]}
        assert counts == {"QA-1": 2, "QA-2": 1}

    @pytest.mark.asyncio
    async def test_errors_are_a_subset_of_unexported(self, async_client, db_session):
        """One condition decides whether the page offers the button, the
        other words it — so they must nest, not partition."""
        sprint = _seed_run_ready_sprint(db_session)
        run = self._seed_run_with_findings(
            db_session,
            sprint,
            [{"tracker_error": "Jira rejected the request (403)"}, {}],
        )

        body = (await async_client.get(f"/api/test-runs/{run.id}")).json()

        assert body["unexported_finding_count"] == 2
        assert body["export_error_count"] == 1
        assert body["exported_finding_count"] == 0

    @pytest.mark.asyncio
    async def test_passing_cases_contribute_nothing(self, async_client, db_session):
        sprint = _seed_run_ready_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(db_session, run, requirement)
        _seed_test_case_execution(
            db_session, execution, requirement.test_plan.cases[0], status="passed"
        )

        body = (await async_client.get(f"/api/test-runs/{run.id}")).json()

        assert body["unexported_finding_count"] == 0
        assert body["exported_finding_count"] == 0

    @pytest.mark.asyncio
    async def test_error_cases_are_not_counted_as_bugs(self, async_client, db_session):
        """An `error` case is an obstruction, not a defect — it never files."""
        sprint = _seed_run_ready_sprint(db_session)
        requirement = _seed_runnable_requirement(db_session, sprint, case_count=1)
        run = _seed_test_run(db_session, sprint)
        execution = _seed_test_execution(db_session, run, requirement)
        _seed_test_case_execution(
            db_session,
            execution,
            requirement.test_plan.cases[0],
            status="error",
            finding_severity="medium",
            finding_title="Could not verify: Login",
            finding_steps_to_reproduce="Open /login",
            finding_expected="The user reaches the dashboard",
            finding_actual="The script never ran",
        )

        body = (await async_client.get(f"/api/test-runs/{run.id}")).json()

        assert body["unexported_finding_count"] == 0
