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
        # The ceiling defaults to the server's maximums, as the modal pre-fills it.
        "max_users": load_runner.NONFUNCTIONAL_LOAD_MAX_CONCURRENCY,
        "max_total_requests": load_runner.NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS,
    }
    body.update(overrides)
    return body


def _generate_body(requirement_id, **overrides):
    body = {
        "requirement_id": requirement_id,
        "max_users": load_runner.NONFUNCTIONAL_LOAD_MAX_CONCURRENCY,
        "max_total_requests": load_runner.NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS,
        "environment_disposable": False,
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
    async def test_returns_proposals_without_restating_the_ceilings(
        self, async_client, db_session, monkeypatch
    ):
        sprint, requirement = _ready_sprint(db_session)
        self._stub_llm(monkeypatch, result=self._result())

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json=_generate_body(requirement.id),
        )

        assert resp.status_code == 200
        data = resp.json()
        # An unknown domain is dropped rather than offered as a checkbox.
        assert [d["domain"] for d in data["domains"]] == ["accessibility"]
        assert data["load_profiles"][0]["method"] == "GET"
        # The ceilings ride on SprintResponse.load_limits now.
        assert "unsafe_max_total_requests" not in data
        assert "max_total_requests" not in data

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
            json=_generate_body(requirement.id),
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

    # ── the run ceiling ───────────────────────────────────────────────
    # Compose is the deterministic half of every prompt rule about the
    # ceiling: the prompt asks, these pin what happens when it is ignored.

    def _proposal(self, **overrides):
        proposal = {
            "base_url_env_var": "BASE_URL",
            "path": "/api/reports",
            "method": "GET",
            "concurrency": 2,
            "duration_seconds": 10,
            "total_request_cap": 50,
            "rationale": "r",
        }
        proposal.update(overrides)
        return proposal

    async def _compose(self, async_client, db_session, monkeypatch, proposals, **body):
        sprint, requirement = _ready_sprint(db_session)
        self._stub_llm(monkeypatch, result=self._result(load_profiles=proposals))
        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json=_generate_body(requirement.id, **body),
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["load_profiles"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ceiling",
        [
            {"max_users": 0},
            {"max_users": 10**6},
            {"max_total_requests": 0},
            {"max_total_requests": 10**9},
        ],
    )
    async def test_a_ceiling_outside_the_server_maximums_is_refused_before_the_llm(
        self, ceiling, async_client, db_session, monkeypatch
    ):
        sprint, requirement = _ready_sprint(db_session)
        calls = self._stub_llm(monkeypatch, result=self._result())

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json=_generate_body(requirement.id, **ceiling),
        )

        assert resp.status_code == 422
        assert calls == []

    @pytest.mark.asyncio
    async def test_the_ceiling_and_the_declaration_reach_the_model(
        self, async_client, db_session, monkeypatch
    ):
        sprint, requirement = _ready_sprint(db_session)
        calls = self._stub_llm(monkeypatch, result=self._result())

        await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json=_generate_body(
                requirement.id, max_users=7, max_total_requests=900, environment_disposable=True
            ),
        )

        assert calls[0]["max_users"] == 7
        assert calls[0]["max_total_requests"] == 900
        assert calls[0]["environment_disposable"] is True

    @pytest.mark.asyncio
    async def test_a_non_safe_proposal_needs_the_declaration(
        self, async_client, db_session, monkeypatch
    ):
        proposals = [self._proposal(method="POST"), self._proposal(method="GET")]

        without = await self._compose(async_client, db_session, monkeypatch, proposals)
        with_it = await self._compose(
            async_client, db_session, monkeypatch, proposals, environment_disposable=True
        )

        assert [p["method"] for p in without] == ["GET"]
        assert [p["method"] for p in with_it] == ["POST", "GET"]

    @pytest.mark.asyncio
    async def test_stress_ramps_to_the_ceiling_and_is_raised_to_its_minimum(
        self, async_client, db_session, monkeypatch
    ):
        from backend.services.load_shapes import min_duration

        (profile,) = await self._compose(
            async_client,
            db_session,
            monkeypatch,
            [self._proposal(shape="stress", concurrency=2, duration_seconds=20)],
            max_users=30,
        )

        assert profile["shape"] == "stress"
        assert profile["concurrency"] == 30
        assert profile["duration_seconds"] == min_duration(
            "stress", routes.NONFUNCTIONAL_LOAD_STRESS_MIN_STEP_SECONDS
        )

    @pytest.mark.asyncio
    async def test_other_shapes_clamp_users_to_the_ceiling(
        self, async_client, db_session, monkeypatch
    ):
        (profile,) = await self._compose(
            async_client,
            db_session,
            monkeypatch,
            [self._proposal(shape="spike", concurrency=40)],
            max_users=25,
        )

        assert profile["concurrency"] == 25

    @pytest.mark.asyncio
    async def test_requests_are_allocated_from_the_run_budget_in_order(
        self, async_client, db_session, monkeypatch
    ):
        profiles = await self._compose(
            async_client,
            db_session,
            monkeypatch,
            [self._proposal(total_request_cap=5000) for _ in range(3)],
            max_total_requests=6000,
        )

        assert [p["total_request_cap"] for p in profiles] == [5000, 1000]

    @pytest.mark.asyncio
    async def test_composed_profiles_fit_the_budget_they_are_created_under(
        self, async_client, db_session, monkeypatch, queue_stub
    ):
        """D16's premise, end to end: what compose allocates, create accepts.

        The task does no budget arithmetic because this holds, so it is pinned
        with a non-default budget rather than assumed.
        """
        sprint, requirement = _ready_sprint(db_session)
        self._stub_llm(
            monkeypatch,
            result=self._result(
                load_profiles=[self._proposal(total_request_cap=5000) for _ in range(3)]
            ),
        )
        ceiling = {"max_users": 10, "max_total_requests": 6000}
        generated = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json=_generate_body(requirement.id, **ceiling),
        )
        assert generated.status_code == 200, generated.text

        created = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                load_profiles=generated.json()["load_profiles"],
                **ceiling,
            ),
        )

        assert created.status_code == 201, created.text
        caps = [p["total_request_cap"] for p in created.json()["load_profiles"]]
        assert sum(caps) <= 6000

    @pytest.mark.asyncio
    async def test_an_unknown_shape_becomes_constant_load(
        self, async_client, db_session, monkeypatch
    ):
        (profile,) = await self._compose(
            async_client, db_session, monkeypatch, [self._proposal(shape="tsunami")]
        )

        assert profile["shape"] == "load"

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
            json=_generate_body(requirement.id),
        )

        assert db_session.exec(routes.select(NonfunctionalRun)).all() == []

    @pytest.mark.asyncio
    async def test_llm_failure_is_a_502(self, async_client, db_session, monkeypatch):
        sprint, requirement = _ready_sprint(db_session)
        self._stub_llm(monkeypatch, error=LLMError("provider down"))

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json=_generate_body(requirement.id),
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
            json=_generate_body(requirement.id),
        )

        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_gate_requires_an_approved_plan(self, async_client, db_session, monkeypatch):
        sprint, requirement = _ready_sprint(db_session, plan_status=TestPlanStatus.DRAFT)
        self._stub_llm(monkeypatch, result=self._result())

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-plan/generate",
            json=_generate_body(requirement.id),
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
            json=_generate_body(requirement.id),
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
            json=_generate_body(requirement.id),
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
                        "concurrency": 99999,
                        "duration_seconds": 99999,
                        "total_request_cap": 99999,
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
    async def test_a_declared_non_safe_profile_clamps_to_the_same_ceiling_as_get(
        self, async_client, db_session, queue_stub
    ):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                environment_disposable=True,
                load_profiles=[
                    {"url": PROFILE_URL, "method": "DELETE", "total_request_cap": 99999}
                ],
            ),
        )

        assert resp.status_code == 201
        stored = resp.json()["load_profiles"][0]
        assert stored["total_request_cap"] == load_runner.NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS

    @pytest.mark.asyncio
    async def test_the_shape_is_persisted_and_the_new_fields_are_echoed(
        self, async_client, db_session, queue_stub
    ):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                load_profiles=[{"url": PROFILE_URL, "method": "GET", "shape": "load"}],
            ),
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["load_profiles"][0]["shape"] == "load"
        assert data["load_profiles"][0]["launched_at"] is None

    @pytest.mark.asyncio
    async def test_the_ceiling_is_persisted_and_echoed(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(requirement_id=requirement.id, max_users=12, max_total_requests=345),
        )

        assert resp.status_code == 201
        data = resp.json()
        assert (data["max_users"], data["max_total_requests"]) == (12, 345)
        stored = db_session.get(NonfunctionalRun, data["id"])
        assert (stored.max_users, stored.max_total_requests) == (12, 345)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ceiling",
        [
            {"max_users": 0},
            {"max_users": 10**6},
            {"max_total_requests": 0},
            {"max_total_requests": 10**9},
        ],
    )
    async def test_a_ceiling_outside_the_server_maximums_is_refused(
        self, ceiling, async_client, db_session, queue_stub
    ):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(requirement_id=requirement.id, **ceiling),
        )

        assert resp.status_code == 422
        assert queue_stub.enqueued == []

    @pytest.mark.asyncio
    async def test_profiles_over_the_run_budget_are_refused_naming_both_numbers(
        self, async_client, db_session, queue_stub
    ):
        """Refused, not trimmed: the budget is what the user consented to (D10)."""
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                max_total_requests=100,
                load_profiles=[
                    {"url": PROFILE_URL, "method": "GET", "total_request_cap": 60},
                    {"url": PROFILE_URL, "method": "HEAD", "total_request_cap": 60},
                ],
            ),
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "120" in detail and "100" in detail
        assert queue_stub.enqueued == []

    @pytest.mark.asyncio
    async def test_a_stress_profile_shorter_than_its_minimum_is_refused(
        self, async_client, db_session, queue_stub
    ):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                load_profiles=[
                    {"url": PROFILE_URL, "method": "GET", "shape": "stress", "duration_seconds": 30}
                ],
            ),
        )

        assert resp.status_code == 422
        assert "at least 50 seconds" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_users_clamp_to_the_ceiling_and_stress_users_are_the_ceiling(
        self, async_client, db_session, queue_stub
    ):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                max_users=5,
                load_profiles=[
                    {"url": PROFILE_URL, "method": "GET", "concurrency": 9},
                    {
                        "url": PROFILE_URL,
                        "method": "GET",
                        "shape": "stress",
                        "concurrency": 1,
                        "duration_seconds": 60,
                    },
                ],
            ),
        )

        assert resp.status_code == 201, resp.text
        assert [p["concurrency"] for p in resp.json()["load_profiles"]] == [5, 5]

    @pytest.mark.asyncio
    async def test_an_unsupported_shape_is_refused(self, async_client, db_session, queue_stub):
        sprint, requirement = _ready_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/nonfunctional-runs",
            json=_create_body(
                requirement_id=requirement.id,
                load_profiles=[{"url": PROFILE_URL, "method": "GET", "shape": "tsunami"}],
            ),
        )

        assert resp.status_code == 422
        assert "tsunami" in resp.json()["detail"]
        assert queue_stub.enqueued == []

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
