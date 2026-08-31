"""Tests for backend/routes/nonfunctional.py — gates, validation, clamping.

The create route is the interesting half: it decides what traffic this
application will put on somebody else's environment, so most of what is
asserted here is a refusal.
"""

import json
from types import SimpleNamespace

import pytest

import backend.routes.nonfunctional as routes
from backend.models.database import (
    NonfunctionalChildStatus,
    NonfunctionalLoadProfile,
    NonfunctionalRun,
    NonfunctionalRunStatus,
    RequirementStatus,
    TestEnvironmentStatus,
    TestPlanStatus,
)
from backend.services import load_runner
from backend.services.llm import LLMError, NonfunctionalPlanResult
from backend.tests.test_nonfunctional_models import (
    _seed_load_profile,
    _seed_nonfunctional_finding,
    _seed_nonfunctional_run,
    _seed_nonfunctional_target,
)
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import _seed_test_case, _seed_test_env, _seed_test_plan

ENV_VARS = {"BASE_URL": "https://staging.example.com", "API_TOKEN": "s3cr3t"}
PROFILE_URL = "https://staging.example.com/api/reports"


@pytest.fixture(autouse=True)
def _public_origins(monkeypatch):
    """The seeded base URL is public; keep the SSRF check from resolving DNS."""
    monkeypatch.setattr(load_runner, "_is_private_host", lambda host: False)


@pytest.fixture
def queue_stub(monkeypatch):
    class _Stub:
        def __init__(self):
            self.enqueued: list[int] = []

        def enqueue_nonfunctional_run(self, run_id):
            self.enqueued.append(run_id)
            return SimpleNamespace(id=f"nf-job-{run_id}")

    stub = _Stub()
    monkeypatch.setattr(routes, "get_queue_service", lambda: stub)
    return stub


def _ready_sprint(db_session, **plan_kwargs):
    """A sprint whose requirement is confirmed, planned and environment-ready."""
    sprint = _seed_sprint(db_session)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    plan = _seed_test_plan(
        db_session, requirement, status=plan_kwargs.pop("plan_status", TestPlanStatus.APPROVED)
    )
    _seed_test_case(db_session, plan)
    _seed_test_env(
        db_session,
        sprint,
        status=TestEnvironmentStatus.CONFIRMED,
        env_vars_json=json.dumps(ENV_VARS),
    )
    db_session.refresh(sprint)
    return sprint, requirement


def _create_body(**overrides):
    body = {
        "requirement_id": None,
        "domains": ["accessibility", "security", "performance"],
        "base_url_env_vars": ["BASE_URL"],
        "load_profiles": [],
        "environment_disposable": False,
        "export_findings": False,
    }
    body.update(overrides)
    return body


# ── POST /sprints/{id}/nonfunctional-plan/generate ────────────────────


class TestGeneratePlan:
    def _stub_llm(self, monkeypatch, result=None, error=None):
        """Records the kwargs, so what the route *hands the model* is assertable."""
        calls: list[dict] = []

        def _generate(**kwargs):
            calls.append(kwargs)
            if error is not None:
                raise error
            return result

        monkeypatch.setattr(routes.llm, "generate_nonfunctional_plan", _generate)
        return calls

    def _result(self, **overrides):
        payload = {
            "domains": [
                {"domain": "accessibility", "applicable": True, "rationale": "It has a UI."},
                {"domain": "made-up", "applicable": True, "rationale": "nonsense"},
            ],
            "base_url_env_vars": ["BASE_URL"],
            "load_profiles": [
                {
                    "base_url_env_var": "BASE_URL",
                    "path": "/api/reports",
                    "method": "get",
                    "body": None,
                    "concurrency": 2,
                    "duration_seconds": 10,
                    "total_request_cap": 50,
                    "rationale": "hot path",
                }
            ],
        }
        payload.update(overrides)
        return NonfunctionalPlanResult(**payload)

    @pytest.mark.asyncio
    async def test_returns_proposals_and_both_ceiling_tiers(
        self, async_client, db_session, monkeypatch
    ):
        sprint, requirement = _ready_sprint(db_session)
        self._stub_llm(monkeypatch, result=self._result())

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json={"requirement_id": requirement.id},
        )

        assert resp.status_code == 200
        data = resp.json()
        # An unknown domain is dropped rather than offered as a checkbox.
        assert [d["domain"] for d in data["domains"]] == ["accessibility"]
        assert data["load_profiles"][0]["method"] == "GET"
        assert data["max_total_requests"] == load_runner.NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS
        assert (
            data["unsafe_max_total_requests"]
            == load_runner.NONFUNCTIONAL_LOAD_UNSAFE_MAX_TOTAL_REQUESTS
        )
        assert set(data["safe_methods"]) == {"GET", "HEAD", "OPTIONS"}

    # ── URL composition ───────────────────────────────────────────────
    # The model gives (variable, path) and never sees a value, so the
    # origin is ours to resolve. These pin that resolution: it is what
    # makes a proposed profile land on a confirmed origin by construction
    # rather than by the model having guessed the host right.

    def _set_env_vars(self, db_session, sprint, env_vars):
        """One TestEnvironmentAccess row per sprint, so re-point the existing one."""
        env = sprint.test_environment
        env.env_vars_json = json.dumps(env_vars)
        db_session.add(env)
        db_session.commit()

    async def _profiles(self, async_client, sprint, requirement):
        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json={"requirement_id": requirement.id},
        )
        assert resp.status_code == 200
        return resp.json()["load_profiles"]

    @pytest.mark.asyncio
    async def test_the_path_is_joined_onto_the_variable_value(
        self, async_client, db_session, monkeypatch
    ):
        sprint, requirement = _ready_sprint(db_session)
        self._stub_llm(monkeypatch, result=self._result())

        profiles = await self._profiles(async_client, sprint, requirement)

        assert profiles[0]["url"] == PROFILE_URL

    @pytest.mark.asyncio
    async def test_a_base_url_path_prefix_survives_the_join(
        self, async_client, db_session, monkeypatch
    ):
        """Not urljoin. urljoin treats a leading slash as root-relative and
        discards the base's own path, silently re-aiming the profile at
        whatever lives at the host root."""
        sprint, requirement = _ready_sprint(db_session)
        self._set_env_vars(db_session, sprint, {"BASE_URL": "https://staging.example.com/app"})
        self._stub_llm(monkeypatch, result=self._result())

        profiles = await self._profiles(async_client, sprint, requirement)

        assert profiles[0]["url"] == "https://staging.example.com/app/api/reports"

    @pytest.mark.asyncio
    async def test_slashes_are_not_doubled(self, async_client, db_session, monkeypatch):
        sprint, requirement = _ready_sprint(db_session)
        self._set_env_vars(db_session, sprint, {"BASE_URL": "https://staging.example.com/"})
        self._stub_llm(monkeypatch, result=self._result())

        profiles = await self._profiles(async_client, sprint, requirement)

        assert profiles[0]["url"] == PROFILE_URL

    @pytest.mark.asyncio
    async def test_an_empty_path_yields_the_base_url_itself(
        self, async_client, db_session, monkeypatch
    ):
        sprint, requirement = _ready_sprint(db_session)
        self._stub_llm(
            monkeypatch,
            result=self._result(
                load_profiles=[
                    {
                        "base_url_env_var": "BASE_URL",
                        "path": "/",
                        "method": "GET",
                        "rationale": "root",
                    }
                ]
            ),
        )

        profiles = await self._profiles(async_client, sprint, requirement)

        assert profiles[0]["url"] == "https://staging.example.com"

    @pytest.mark.asyncio
    async def test_a_profile_naming_an_unnominated_variable_is_dropped(
        self, async_client, db_session, monkeypatch
    ):
        """Composing against an origin nobody nominated is worse than
        proposing nothing — and API_TOKEN is not even a URL."""
        sprint, requirement = _ready_sprint(db_session)
        self._stub_llm(
            monkeypatch,
            result=self._result(
                load_profiles=[
                    {
                        "base_url_env_var": "API_TOKEN",
                        "path": "/x",
                        "method": "GET",
                        "rationale": "no",
                    },
                    {
                        "base_url_env_var": "BASE_URL",
                        "path": "/api/reports",
                        "method": "GET",
                        "rationale": "yes",
                    },
                ]
            ),
        )

        profiles = await self._profiles(async_client, sprint, requirement)

        assert [p["url"] for p in profiles] == [PROFILE_URL]

    @pytest.mark.asyncio
    async def test_a_composed_profile_survives_the_create_route(
        self, async_client, db_session, monkeypatch, queue_stub
    ):
        """The whole point: Start without editing must not 422. This is the
        end-to-end shape the original bug broke — a proposal the app's own
        validator would refuse."""
        sprint, requirement = _ready_sprint(db_session)
        self._stub_llm(monkeypatch, result=self._result())
        profiles = await self._profiles(async_client, sprint, requirement)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(requirement_id=requirement.id, load_profiles=profiles),
        )

        assert resp.status_code == 201, resp.text

    # ── read_file wiring ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_no_file_tree_means_no_read_file(self, async_client, db_session, monkeypatch):
        """Degrades to a plain completion rather than raising."""
        sprint, requirement = _ready_sprint(db_session)
        calls = self._stub_llm(monkeypatch, result=self._result())

        await self._profiles(async_client, sprint, requirement)

        assert calls[0]["read_file"] is None

    @pytest.mark.asyncio
    async def test_a_file_tree_supplies_a_read_file_executor(
        self, async_client, db_session, monkeypatch
    ):
        sprint, requirement = _ready_sprint(db_session)
        sprint.repo.file_tree = "backend/routes/reports.py"
        db_session.add(sprint.repo)
        db_session.commit()
        calls = self._stub_llm(monkeypatch, result=self._result())

        await self._profiles(async_client, sprint, requirement)

        assert callable(calls[0]["read_file"])

    @pytest.mark.asyncio
    async def test_variable_names_are_split_and_no_value_is_sent(
        self, async_client, db_session, monkeypatch
    ):
        """The design decision, pinned at the route boundary too."""
        sprint, requirement = _ready_sprint(db_session)
        calls = self._stub_llm(monkeypatch, result=self._result())

        await self._profiles(async_client, sprint, requirement)

        assert calls[0]["url_env_var_names"] == ["BASE_URL"]
        assert calls[0]["other_env_var_names"] == ["API_TOKEN"]
        assert "env_vars" not in calls[0]

    @pytest.mark.asyncio
    async def test_persists_nothing(self, async_client, db_session, monkeypatch):
        sprint, requirement = _ready_sprint(db_session)
        self._stub_llm(monkeypatch, result=self._result())

        await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json={"requirement_id": requirement.id},
        )

        assert db_session.exec(routes.select(NonfunctionalRun)).all() == []

    @pytest.mark.asyncio
    async def test_llm_failure_is_a_502(self, async_client, db_session, monkeypatch):
        sprint, requirement = _ready_sprint(db_session)
        self._stub_llm(monkeypatch, error=LLMError("provider down"))

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json={"requirement_id": requirement.id},
        )

        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_a_nominated_variable_that_is_not_a_url_is_a_502(
        self, async_client, db_session, monkeypatch
    ):
        """The model only ever saw names — its nomination is checked here."""
        sprint, requirement = _ready_sprint(db_session)
        self._stub_llm(monkeypatch, result=self._result(base_url_env_vars=["API_TOKEN"]))

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json={"requirement_id": requirement.id},
        )

        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_gate_requires_an_approved_plan(self, async_client, db_session, monkeypatch):
        sprint, requirement = _ready_sprint(db_session, plan_status=TestPlanStatus.DRAFT)
        self._stub_llm(monkeypatch, result=self._result())

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json={"requirement_id": requirement.id},
        )

        assert resp.status_code == 422
        assert "approved test plan" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_gate_requires_a_confirmed_environment(
        self, async_client, db_session, monkeypatch
    ):
        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED)
        db_session.refresh(sprint)
        self._stub_llm(monkeypatch, result=self._result())

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json={"requirement_id": requirement.id},
        )

        assert resp.status_code == 422
        assert "test environment" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_gate_refuses_a_finished_sprint(self, async_client, db_session, monkeypatch):
        sprint, requirement = _ready_sprint(db_session)
        sprint.active = False
        db_session.add(sprint)
        db_session.commit()
        self._stub_llm(monkeypatch, result=self._result())

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json={"requirement_id": requirement.id},
        )

        assert resp.status_code == 422
        assert "Sprint is finished" in resp.json()["detail"]


# ── POST /sprints/{id}/nonfunctional-runs ─────────────────────────────


class TestCreateRun:
    @pytest.mark.asyncio
    async def test_creates_a_run_and_enqueues_it(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(requirement_id=requirement.id),
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == NonfunctionalRunStatus.PENDING
        assert data["domains"] == ["accessibility", "security", "performance"]
        assert data["base_url_env_vars"] == ["BASE_URL"]
        assert queue_stub.enqueued == [data["id"]]

    @pytest.mark.asyncio
    async def test_zero_domains_is_refused_by_name(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(requirement_id=requirement.id, domains=[]),
        )

        assert resp.status_code == 422
        assert "at least one domain" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_an_unknown_domain_is_refused(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(requirement_id=requirement.id, domains=["telepathy"]),
        )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_a_non_safe_method_needs_the_declaration(
        self, async_client, db_session, queue_stub
    ):
        sprint, requirement = _ready_sprint(db_session)
        profile = {"url": PROFILE_URL, "method": "POST", "body": None}

        refused = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(requirement_id=requirement.id, load_profiles=[profile]),
        )
        assert refused.status_code == 422
        assert "disposable" in refused.json()["detail"]

        allowed = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                load_profiles=[profile],
                environment_disposable=True,
            ),
        )
        assert allowed.status_code == 201
        assert allowed.json()["environment_disposable"] is True

    @pytest.mark.asyncio
    async def test_ceilings_are_clamped_and_echoed_back(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                load_profiles=[
                    {
                        "url": PROFILE_URL,
                        "method": "GET",
                        "concurrency": 9999,
                        "duration_seconds": 9999,
                        "total_request_cap": 9999,
                    }
                ],
            ),
        )

        assert resp.status_code == 201
        stored = resp.json()["load_profiles"][0]
        assert stored["concurrency"] == load_runner.NONFUNCTIONAL_LOAD_MAX_CONCURRENCY
        assert stored["duration_seconds"] == load_runner.NONFUNCTIONAL_LOAD_MAX_DURATION_SECONDS
        assert stored["total_request_cap"] == load_runner.NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS

    @pytest.mark.asyncio
    async def test_the_unsafe_tier_clamps_lower(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                environment_disposable=True,
                load_profiles=[{"url": PROFILE_URL, "method": "DELETE", "total_request_cap": 9999}],
            ),
        )

        stored = resp.json()["load_profiles"][0]
        assert (
            stored["total_request_cap"] == load_runner.NONFUNCTIONAL_LOAD_UNSAFE_MAX_TOTAL_REQUESTS
        )

    @pytest.mark.asyncio
    async def test_an_off_origin_load_url_is_refused(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                load_profiles=[{"url": "https://elsewhere.example.com/api", "method": "GET"}],
            ),
        )

        assert resp.status_code == 422
        assert "confirmed origins" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_loopback_load_url_is_refused(
        self, async_client, db_session, queue_stub, monkeypatch
    ):
        """The route half of the SSRF refusal — this app's own API included."""
        monkeypatch.undo()  # restore the real _is_private_host
        env = {**ENV_VARS, "BASE_URL": "http://localhost:8000"}
        sprint = _seed_sprint(db_session)
        requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
        _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED)
        _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.CONFIRMED,
            env_vars_json=json.dumps(env),
        )
        db_session.refresh(sprint)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                load_profiles=[{"url": "http://localhost:8000/api/health", "method": "GET"}],
            ),
        )

        assert resp.status_code == 422
        assert "private address space" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_an_unknown_placeholder_in_a_body_is_refused(
        self, async_client, db_session, queue_stub
    ):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                environment_disposable=True,
                load_profiles=[{"url": PROFILE_URL, "method": "POST", "body": '{"t": "$NOPE"}'}],
            ),
        )

        assert resp.status_code == 422
        assert "NOPE" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_too_many_profiles_are_refused(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)
        profiles = [{"url": PROFILE_URL, "method": "GET"}] * (
            routes.NONFUNCTIONAL_MAX_LOAD_PROFILES + 1
        )

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(requirement_id=requirement.id, load_profiles=profiles),
        )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_export_without_a_tracker_is_refused(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(requirement_id=requirement.id, export_findings=True),
        )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_a_second_in_progress_run_is_refused(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)
        _seed_nonfunctional_run(
            db_session, sprint, requirement, status=NonfunctionalRunStatus.RUNNING
        )

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(requirement_id=requirement.id),
        )

        assert resp.status_code == 422
        assert "already has a nonfunctional run in progress" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_the_revision_triple_is_recorded(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(requirement_id=requirement.id),
        )

        run = db_session.get(NonfunctionalRun, resp.json()["id"])
        assert run.requirement_revision == requirement.content_revision
        assert run.plan_revision == requirement.test_plan.content_revision
        assert run.env_revision == sprint.test_environment.content_revision
        assert resp.json()["outdated_reasons"] == []


# ── reads ─────────────────────────────────────────────────────────────


class TestReads:
    @pytest.mark.asyncio
    async def test_list_and_detail(self, async_client, db_session):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_nonfunctional_run(db_session, sprint, requirement)
        target = _seed_nonfunctional_target(db_session, run)
        _seed_load_profile(db_session, run)
        _seed_nonfunctional_finding(db_session, target)

        listed = await async_client.get(f"/api/sprints/{sprint.id}/nonfunctional-runs")
        assert listed.status_code == 200
        assert listed.json()[0]["bug_count"] == 1
        assert listed.json()[0]["target_count"] == 1

        detail = await async_client.get(f"/api/nonfunctional-runs/{run.id}")
        assert detail.status_code == 200
        data = detail.json()
        assert len(data["targets"]) == 1
        assert len(data["load_profiles"]) == 1
        assert data["findings"][0]["rule"] == "image-alt"
        assert data["findings"][0]["url"] == target.url

    @pytest.mark.asyncio
    async def test_a_malformed_metrics_blob_renders_as_empty(self, async_client, db_session):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_nonfunctional_run(db_session, sprint, requirement)
        _seed_nonfunctional_target(db_session, run, metrics_json="{not json")

        resp = await async_client.get(f"/api/nonfunctional-runs/{run.id}")

        assert resp.status_code == 200
        assert resp.json()["targets"][0]["metrics"] == {}

    @pytest.mark.asyncio
    async def test_a_missing_run_404s(self, async_client, db_session):
        assert (await async_client.get("/api/nonfunctional-runs/9999")).status_code == 404

    @pytest.mark.asyncio
    async def test_screenshot_404s_when_the_finding_carries_none(self, async_client, db_session):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_nonfunctional_run(db_session, sprint, requirement)
        target = _seed_nonfunctional_target(db_session, run)
        finding = _seed_nonfunctional_finding(db_session, target)

        resp = await async_client.get(f"/api/nonfunctional-findings/{finding.id}/screenshot")

        assert resp.status_code == 404


# ── restart / summarize / export ──────────────────────────────────────


class TestRestart:
    @pytest.mark.asyncio
    async def test_a_failed_run_restarts(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_nonfunctional_run(
            db_session,
            sprint,
            requirement,
            status=NonfunctionalRunStatus.FAILED,
            error="boom",
            retry_count=3,
        )

        resp = await async_client.post(f"/api/nonfunctional-runs/{run.id}/restart")

        assert resp.status_code == 200
        assert resp.json()["status"] == NonfunctionalRunStatus.PENDING
        assert resp.json()["error"] is None
        assert queue_stub.enqueued == [run.id]

    @pytest.mark.asyncio
    async def test_a_completed_run_cannot_restart(self, async_client, db_session):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_nonfunctional_run(
            db_session, sprint, requirement, status=NonfunctionalRunStatus.COMPLETED
        )

        resp = await async_client.post(f"/api/nonfunctional-runs/{run.id}/restart")

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_an_outdated_run_cannot_restart(self, async_client, db_session):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_nonfunctional_run(
            db_session,
            sprint,
            requirement,
            status=NonfunctionalRunStatus.FAILED,
            requirement_revision=-1,
        )

        resp = await async_client.post(f"/api/nonfunctional-runs/{run.id}/restart")

        assert resp.status_code == 422
        assert "requirement" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_restart_does_not_touch_a_profile_that_already_sent_traffic(
        self, async_client, db_session, queue_stub
    ):
        """The route never rewrites child rows — the invariant lives in the task."""
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_nonfunctional_run(
            db_session, sprint, requirement, status=NonfunctionalRunStatus.FAILED
        )
        profile = _seed_load_profile(
            db_session, run, requests_sent=20, status=NonfunctionalChildStatus.COMPLETED
        )

        await async_client.post(f"/api/nonfunctional-runs/{run.id}/restart")

        db_session.expire_all()
        stored = db_session.get(NonfunctionalLoadProfile, profile.id)
        assert stored.requests_sent == 20
        assert stored.status == NonfunctionalChildStatus.COMPLETED


class TestSummarize:
    @pytest.mark.asyncio
    async def test_a_non_completed_run_is_refused(self, async_client, db_session):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_nonfunctional_run(
            db_session, sprint, requirement, status=NonfunctionalRunStatus.RUNNING
        )

        resp = await async_client.post(f"/api/nonfunctional-runs/{run.id}/summarize")

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_a_completed_run_is_summarized(self, async_client, db_session, monkeypatch):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_nonfunctional_run(
            db_session, sprint, requirement, status=NonfunctionalRunStatus.COMPLETED
        )
        _seed_nonfunctional_target(db_session, run)
        monkeypatch.setattr(
            routes.llm,
            "summarize_nonfunctional",
            lambda **kwargs: SimpleNamespace(summary="All clean."),
        )

        resp = await async_client.post(f"/api/nonfunctional-runs/{run.id}/summarize")

        assert resp.status_code == 200
        assert resp.json()["summary"] == "All clean."

    @pytest.mark.asyncio
    async def test_an_llm_failure_is_a_502(self, async_client, db_session, monkeypatch):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_nonfunctional_run(
            db_session, sprint, requirement, status=NonfunctionalRunStatus.COMPLETED
        )

        def _boom(**kwargs):
            raise LLMError("down")

        monkeypatch.setattr(routes.llm, "summarize_nonfunctional", _boom)

        resp = await async_client.post(f"/api/nonfunctional-runs/{run.id}/summarize")

        assert resp.status_code == 502


class TestExportFindings:
    @pytest.mark.asyncio
    async def test_refused_with_no_tracker_connected(self, async_client, db_session):
        sprint, requirement = _ready_sprint(db_session)
        run = _seed_nonfunctional_run(
            db_session, sprint, requirement, status=NonfunctionalRunStatus.COMPLETED
        )

        resp = await async_client.post(f"/api/nonfunctional-runs/{run.id}/export-findings")

        assert resp.status_code == 422
