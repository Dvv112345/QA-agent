"""Tests for backend/routes/test_environment.py — the synchronous LLM-judged flow.

LLM functions are monkeypatched on ``backend.services.llm`` (the route calls
them as module attributes) and README resolution is stubbed out on the route
module — no network, no Redis.
"""

import json
from datetime import datetime, timezone

import pytest

from backend.models.database import (
    RequirementStatus,
    TestEnvironmentAccess,
    TestEnvironmentStatus,
)
from backend.services.llm import EnvVarsResult, LLMError, TestEnvironmentResult
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import _seed_test_env

SUFFICIENT = TestEnvironmentResult(sufficient=True, clarifying_question=None)
INSUFFICIENT = TestEnvironmentResult(
    sufficient=False, clarifying_question="What are the credentials?"
)
DEFAULT_ENV_VARS = EnvVarsResult(variables={"BASE_URL": "https://staging.example.com"})

EARLIER = datetime(2026, 7, 1, tzinfo=timezone.utc)
LATER = datetime(2026, 7, 2, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_readme(monkeypatch):
    """Keep README resolution deterministic: no disk reads, no GitHub calls."""
    import backend.routes.test_environment as te_module

    async def _none(sprint):
        return None

    monkeypatch.setattr(te_module, "resolve_readme", _none)


@pytest.fixture
def llm_stub(monkeypatch):
    """Replace the three test-env LLM entry points with a recording stub.

    ``stub.check_result`` / ``stub.revise_result`` / ``stub.env_vars_result``
    may be their respective result type or an exception to raise.
    """

    class _Stub:
        def __init__(self):
            self.check_result = SUFFICIENT
            self.revise_result = TestEnvironmentResult(
                sufficient=True,
                clarifying_question=None,
                rewritten_content="Rewritten access text.",
            )
            self.env_vars_result = DEFAULT_ENV_VARS
            self.check_calls: list[dict] = []
            self.revise_calls: list[dict] = []
            self.env_vars_calls: list[dict] = []

        @staticmethod
        def _resolve(result):
            if isinstance(result, Exception):
                raise result
            return result

        def check_test_environment(self, content, requirements, readme, file_tree):
            self.check_calls.append(
                {
                    "content": content,
                    "requirements": requirements,
                    "readme": readme,
                    "file_tree": file_tree,
                }
            )
            return self._resolve(self.check_result)

        def revise_test_environment(
            self, content, question, answer, requirements, readme, file_tree
        ):
            self.revise_calls.append(
                {
                    "content": content,
                    "question": question,
                    "answer": answer,
                    "requirements": requirements,
                    "readme": readme,
                    "file_tree": file_tree,
                }
            )
            return self._resolve(self.revise_result)

        def generate_env_vars(self, content, readme, file_tree):
            self.env_vars_calls.append(
                {"content": content, "readme": readme, "file_tree": file_tree}
            )
            return self._resolve(self.env_vars_result)

    stub = _Stub()
    import backend.services.llm as llm_module

    monkeypatch.setattr(llm_module, "check_test_environment", stub.check_test_environment)
    monkeypatch.setattr(llm_module, "revise_test_environment", stub.revise_test_environment)
    monkeypatch.setattr(llm_module, "generate_env_vars", stub.generate_env_vars)
    return stub


def _seed_complete_sprint(db_session, active: bool = True):
    """Sprint with one confirmed requirement — ready for a test-env submission."""
    sprint = _seed_sprint(db_session, active=active)
    _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    return sprint


def _reload(db_session, te_id) -> TestEnvironmentAccess | None:
    db_session.expire_all()
    return db_session.get(TestEnvironmentAccess, te_id)


# ── GET /api/sprints/{id}/test-environment ───────────────────────────


class TestGetTestEnvironment:
    @pytest.mark.asyncio
    async def test_404_for_missing_sprint(self, async_client):
        resp = await async_client.get("/api/sprints/99999/test-environment")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Sprint not found."

    @pytest.mark.asyncio
    async def test_404_when_no_submission(self, async_client, db_session):
        sprint = _seed_complete_sprint(db_session)
        resp = await async_client.get(f"/api/sprints/{sprint.id}/test-environment")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "No test environment submission for this sprint."

    @pytest.mark.asyncio
    async def test_returns_row(self, async_client, db_session):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, clarifying_question="Which host?")

        resp = await async_client.get(f"/api/sprints/{sprint.id}/test-environment")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == row.id
        assert data["sprint_id"] == sprint.id
        assert data["status"] == "needs_info"
        assert data["clarifying_question"] == "Which host?"
        assert data["clarification_cap_reached"] is False
        assert data["requirements_stale"] is False

    @pytest.mark.asyncio
    async def test_readable_on_finished_sprint(self, async_client, db_session):
        sprint = _seed_complete_sprint(db_session, active=False)
        _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)

        resp = await async_client.get(f"/api/sprints/{sprint.id}/test-environment")

        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"


# ── POST /api/sprints/{id}/test-environment ──────────────────────────


class TestSubmitTestEnvironment:
    @pytest.mark.asyncio
    async def test_sufficient_creates_ready_row(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment",
            json={"content": "SSH to staging.example.com as qa."},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["content"] == "SSH to staging.example.com as qa."
        assert data["original_content"] == "SSH to staging.example.com as qa."
        assert data["clarifying_question"] is None
        assert data["revision_count"] == 0

    @pytest.mark.asyncio
    async def test_insufficient_creates_needs_info_row(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        llm_stub.check_result = INSUFFICIENT

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment",
            json={"content": "SSH to staging."},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "needs_info"
        assert data["clarifying_question"] == "What are the credentials?"

    @pytest.mark.asyncio
    async def test_404_for_missing_sprint(self, async_client, llm_stub):
        resp = await async_client.post(
            "/api/sprints/99999/test-environment", json={"content": "SSH."}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_422_on_finished_sprint(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session, active=False)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "SSH."}
        )

        assert resp.status_code == 422
        assert llm_stub.check_calls == []

    @pytest.mark.asyncio
    async def test_422_with_zero_requirements(self, async_client, db_session, llm_stub):
        sprint = _seed_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "SSH."}
        )

        assert resp.status_code == 422
        assert "requirements must be confirmed" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_with_non_confirmed_requirement(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.READY, name="Search")

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "SSH."}
        )

        assert resp.status_code == 422
        assert llm_stub.check_calls == []

    @pytest.mark.asyncio
    async def test_resubmit_allowed_once_confirmed(self, async_client, db_session, llm_stub):
        """Confirmation is no longer terminal — a resubmit re-runs the check."""
        sprint = _seed_complete_sprint(db_session)
        _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "New text."}
        )

        assert resp.status_code == 200
        assert resp.json()["status"] != TestEnvironmentStatus.CONFIRMED
        assert llm_stub.check_calls  # the check actually ran

    @pytest.mark.asyncio
    async def test_insufficient_recheck_clears_variables_and_cascades(
        self, async_client, db_session, llm_stub
    ):
        """Clearing the extracted variables is itself a content change.

        Re-checking unchanged text does not cascade on the *text* — but an
        insufficient verdict nulls `env_vars_json`, and those are what runs
        execute against. Leaving the plans approved and the runs reporting
        current while the variables vanish is the state this guards.
        """
        from backend.models.database import TestPlan, TestPlanStatus
        from backend.tests.test_sprints import _seed_test_plan

        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.CONFIRMED,
            env_vars_json=json.dumps(DEFAULT_ENV_VARS.variables),
        )
        plan_id = _seed_test_plan(
            db_session, sprint.requirements[0], status=TestPlanStatus.APPROVED
        ).id
        llm_stub.check_result = INSUFFICIENT

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": row.content}
        )

        assert resp.status_code == 200
        db_session.expire_all()
        reloaded = db_session.get(TestEnvironmentAccess, row.id)
        assert reloaded.env_vars_json is None
        assert reloaded.content_revision == 1
        assert db_session.get(TestPlan, plan_id) is None

    @pytest.mark.asyncio
    async def test_answer_rewrite_cascades(self, async_client, db_session, llm_stub):
        """The rewrite from an answer is a content change like any other.

        Seeded directly into `needs_info`-with-plans: reaching that state
        through the API is no longer possible, because whatever put the row
        into `needs_info` already cleared the variables and cascaded. This
        keeps the rule pinned in the one place it lives rather than relying
        on that remaining true.
        """
        from backend.models.database import TestPlan, TestPlanStatus
        from backend.tests.test_sprints import _seed_test_plan

        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.NEEDS_INFO,
            clarifying_question="Which host?",
            env_vars_json=json.dumps(DEFAULT_ENV_VARS.variables),
        )
        plan_id = _seed_test_plan(
            db_session, sprint.requirements[0], status=TestPlanStatus.APPROVED
        ).id

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer", json={"answer": "Creds are in vault."}
        )

        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(TestPlan, plan_id) is None
        assert db_session.get(TestEnvironmentAccess, row.id).content_revision == 1

    @pytest.mark.asyncio
    async def test_answer_that_rewrites_nothing_keeps_the_plans(
        self, async_client, db_session, llm_stub
    ):
        from backend.models.database import TestPlan, TestPlanStatus
        from backend.tests.test_sprints import _seed_test_plan

        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.NEEDS_INFO)
        plan_id = _seed_test_plan(
            db_session, sprint.requirements[0], status=TestPlanStatus.APPROVED
        ).id
        llm_stub.revise_result = TestEnvironmentResult(
            sufficient=False,
            clarifying_question="Still need the host.",
            rewritten_content=row.content,
        )

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer", json={"answer": "No change needed."}
        )

        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(TestPlan, plan_id) is not None
        assert db_session.get(TestEnvironmentAccess, row.id).content_revision == 0

    @pytest.mark.asyncio
    async def test_content_change_removes_every_plan(self, async_client, db_session, llm_stub):
        from backend.models.database import TestPlan, TestPlanStatus
        from backend.tests.test_sprints import _seed_test_plan

        sprint = _seed_complete_sprint(db_session)
        _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)
        plan_ids = [
            _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED).id
            for requirement in sprint.requirements
        ]
        assert plan_ids

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "Different access."}
        )

        assert resp.status_code == 200
        db_session.expire_all()
        assert all(db_session.get(TestPlan, plan_id) is None for plan_id in plan_ids)

    @pytest.mark.asyncio
    async def test_rechecking_the_same_text_keeps_the_plans(
        self, async_client, db_session, llm_stub
    ):
        """The Re-check button resubmits unchanged text — not a content change."""
        from backend.models.database import TestPlan, TestPlanStatus
        from backend.tests.test_sprints import _seed_test_plan

        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.CONFIRMED,
            env_vars_json=json.dumps(DEFAULT_ENV_VARS.variables),
        )
        plan_id = _seed_test_plan(
            db_session, sprint.requirements[0], status=TestPlanStatus.APPROVED
        ).id

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": row.content}
        )

        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(TestPlan, plan_id) is not None
        assert db_session.get(TestEnvironmentAccess, row.id).content_revision == 0

    @pytest.mark.asyncio
    async def test_recheck_does_not_re_extract_unchanged_text(
        self, async_client, db_session, llm_stub
    ):
        """Unchanged text keeps its stored variables — no second LLM call.

        The sufficiency check is the point of Re-check and still runs; the
        extraction is skipped because the variables are derived from a
        description that did not move.
        """
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.READY,
            env_vars_json=json.dumps(DEFAULT_ENV_VARS.variables),
        )

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": row.content}
        )

        assert resp.status_code == 200
        assert llm_stub.check_calls  # the judgment ran
        assert llm_stub.env_vars_calls == []  # the extraction did not

    @pytest.mark.asyncio
    async def test_recheck_survives_a_drifting_extractor(self, async_client, db_session, llm_stub):
        """A re-worded extraction of identical text must not destroy the plans.

        The comparison in `_apply_check_result` cannot catch this — the model
        is free to rename a key or add a variable on any call, and any drift
        reads as a content change. Not calling it at all is what holds.
        """
        from backend.models.database import TestPlan, TestPlanStatus
        from backend.tests.test_sprints import _seed_test_plan

        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.CONFIRMED,
            env_vars_json=json.dumps({"BASE_URL": "https://staging.example.com"}),
        )
        plan_id = _seed_test_plan(
            db_session, sprint.requirements[0], status=TestPlanStatus.APPROVED
        ).id
        # Same environment, different words for it — the realistic failure.
        llm_stub.env_vars_result = EnvVarsResult(
            variables={"APP_URL": "https://staging.example.com/", "TIMEOUT": "30"}
        )

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": row.content}
        )

        assert resp.status_code == 200
        db_session.expire_all()
        refreshed = db_session.get(TestEnvironmentAccess, row.id)
        assert refreshed.content_revision == 0
        assert refreshed.env_vars == {"BASE_URL": "https://staging.example.com"}
        assert db_session.get(TestPlan, plan_id) is not None

    @pytest.mark.asyncio
    async def test_recheck_preserves_hand_edited_variables(
        self, async_client, db_session, llm_stub
    ):
        """A manual correction must outlive the next Re-check.

        Re-extracting would overwrite it silently — no warning, no record.
        """
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.READY,
            env_vars_json=json.dumps(DEFAULT_ENV_VARS.variables),
        )
        corrected = {"BASE_URL": "https://staging.internal.example.com"}
        patch_resp = await async_client.patch(
            f"/api/test-environment/{row.id}/env-vars", json={"variables": corrected}
        )
        assert patch_resp.status_code == 200

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": row.content}
        )

        assert resp.status_code == 200
        assert resp.json()["env_vars"] == corrected
        assert _reload(db_session, row.id).env_vars == corrected

    @pytest.mark.asyncio
    async def test_changed_text_still_re_extracts(self, async_client, db_session, llm_stub):
        """The skip is scoped to identical text — new text is a new derivation."""
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.READY,
            env_vars_json=json.dumps({"BASE_URL": "https://old.example.com"}),
        )
        llm_stub.env_vars_result = EnvVarsResult(variables={"BASE_URL": "https://new.example.com"})

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment",
            json={"content": f"{row.content} Now also reachable at https://new.example.com."},
        )

        assert resp.status_code == 200
        assert len(llm_stub.env_vars_calls) == 1
        refreshed = _reload(db_session, row.id)
        assert refreshed.env_vars == {"BASE_URL": "https://new.example.com"}
        assert refreshed.content_revision == 1

    @pytest.mark.asyncio
    async def test_unchanged_text_extracts_when_the_row_has_no_variables(
        self, async_client, db_session, llm_stub
    ):
        """Reachable on identical text: a verdict can flip without the text moving.

        The requirements this description is judged against changed, so a
        description that was insufficient (variables cleared to None) can now
        come back sufficient — and must get an extraction.
        """
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.NEEDS_INFO,
            env_vars_json=None,
        )

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": row.content}
        )

        assert resp.status_code == 200
        assert len(llm_stub.env_vars_calls) == 1
        assert _reload(db_session, row.id).env_vars == DEFAULT_ENV_VARS.variables

    @pytest.mark.asyncio
    async def test_422_on_blank_content(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "   "}
        )

        assert resp.status_code == 422
        assert llm_stub.check_calls == []

    @pytest.mark.asyncio
    async def test_edit_updates_content_and_clears_question(
        self, async_client, db_session, llm_stub
    ):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            content="SSH to staging.",
            original_content="SSH to staging.",
            clarifying_question="Which host?",
            revision_count=2,
        )

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment",
            json={"content": "SSH to staging.example.com as qa."},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == row.id  # upsert, not a second row
        assert data["content"] == "SSH to staging.example.com as qa."
        assert data["original_content"] == "SSH to staging."
        assert data["clarifying_question"] is None
        assert data["revision_count"] == 2  # direct edit never touches the cap counter

    @pytest.mark.asyncio
    async def test_llm_error_persists_nothing_on_create(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        llm_stub.check_result = LLMError("provider down")

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "SSH."}
        )

        assert resp.status_code == 502
        get_resp = await async_client.get(f"/api/sprints/{sprint.id}/test-environment")
        assert get_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_llm_error_leaves_existing_row_unchanged(
        self, async_client, db_session, llm_stub
    ):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, content="Original text.")
        llm_stub.check_result = LLMError("provider down")

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "New text."}
        )

        assert resp.status_code == 502
        fresh = _reload(db_session, row.id)
        assert fresh.content == "Original text."
        assert fresh.status == TestEnvironmentStatus.NEEDS_INFO

    @pytest.mark.asyncio
    async def test_resolve_readme_none_still_checks(self, async_client, db_session, llm_stub):
        # The autouse fixture already forces resolve_readme → None.
        sprint = _seed_complete_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "SSH."}
        )

        assert resp.status_code == 200
        assert llm_stub.check_calls[0]["readme"] is None

    @pytest.mark.asyncio
    async def test_check_receives_current_confirmed_requirements(
        self, async_client, db_session, llm_stub
    ):
        sprint = _seed_complete_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "SSH."}
        )
        assert resp.status_code == 200
        assert len(llm_stub.check_calls[0]["requirements"]) == 1

        _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.CONFIRMED,
            name="Search",
            description="Users can search.",
        )

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "SSH again."}
        )
        assert resp.status_code == 200
        assert llm_stub.check_calls[1]["requirements"] == [
            ("Login", "Users can log in."),
            ("Search", "Users can search."),
        ]

    @pytest.mark.asyncio
    async def test_file_tree_passed_from_repo(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        sprint.repo.file_tree = "src/app.py\nsrc/db.py"
        db_session.add(sprint.repo)
        db_session.commit()

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "SSH."}
        )

        assert resp.status_code == 200
        assert llm_stub.check_calls[0]["file_tree"] == "src/app.py\nsrc/db.py"


# ── POST /api/test-environment/{id}/answer ───────────────────────────


class TestAnswerTestEnvironment:
    @pytest.mark.asyncio
    async def test_happy_path_rewrites_and_increments(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, clarifying_question="Which host?")

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer",
            json={"answer": "staging.example.com"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "Rewritten access text."
        assert data["original_content"] == row.original_content
        assert data["revision_count"] == 1
        assert data["status"] == "ready"
        assert data["clarifying_question"] is None
        assert llm_stub.revise_calls[0]["question"] == "Which host?"
        assert llm_stub.revise_calls[0]["answer"] == "staging.example.com"

    @pytest.mark.asyncio
    async def test_still_insufficient_keeps_needs_info(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, clarifying_question="Which host?")
        llm_stub.revise_result = TestEnvironmentResult(
            sufficient=False,
            clarifying_question="And the port?",
            rewritten_content="Rewritten.",
        )

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer", json={"answer": "staging"}
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "needs_info"
        assert data["clarifying_question"] == "And the port?"
        assert data["revision_count"] == 1

    @pytest.mark.asyncio
    async def test_404_for_missing_row(self, async_client, llm_stub):
        resp = await async_client.post("/api/test-environment/99999/answer", json={"answer": "x"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_422_on_finished_sprint(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session, active=False)
        row = _seed_test_env(db_session, sprint, clarifying_question="Which host?")

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer", json={"answer": "x"}
        )

        assert resp.status_code == 422
        assert llm_stub.revise_calls == []

    @pytest.mark.asyncio
    async def test_422_with_non_confirmed_requirement(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, clarifying_question="Which host?")
        _seed_requirement(db_session, sprint, status=RequirementStatus.READY, name="Search")

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer", json={"answer": "x"}
        )

        assert resp.status_code == 422
        assert "requirements must be confirmed" in resp.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status", [TestEnvironmentStatus.READY, TestEnvironmentStatus.CONFIRMED]
    )
    async def test_422_when_not_needs_info(self, async_client, db_session, llm_stub, status):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, status=status)

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer", json={"answer": "x"}
        )

        assert resp.status_code == 422
        assert "awaiting more information" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_past_cap(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session, sprint, clarifying_question="Which host?", revision_count=3
        )

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer", json={"answer": "x"}
        )

        assert resp.status_code == 422
        assert "edit the text directly" in resp.json()["detail"]
        assert llm_stub.revise_calls == []

    @pytest.mark.asyncio
    async def test_422_on_empty_answer(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, clarifying_question="Which host?")

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer", json={"answer": "   "}
        )

        assert resp.status_code == 422
        assert llm_stub.revise_calls == []

    @pytest.mark.asyncio
    async def test_llm_error_leaves_row_unchanged(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, clarifying_question="Which host?")
        llm_stub.revise_result = LLMError("provider down")

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer", json={"answer": "staging"}
        )

        assert resp.status_code == 502
        fresh = _reload(db_session, row.id)
        assert fresh.revision_count == 0
        assert fresh.status == TestEnvironmentStatus.NEEDS_INFO
        assert fresh.clarifying_question == "Which host?"


# ── Env-var extraction wiring (submit/answer) ─────────────────────────


class TestEnvVarsExtraction:
    @pytest.mark.asyncio
    async def test_sufficient_submission_extracts_vars(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment",
            json={"content": "SSH to staging.example.com as qa."},
        )

        assert resp.status_code == 200
        assert resp.json()["env_vars"] == {"BASE_URL": "https://staging.example.com"}
        assert len(llm_stub.env_vars_calls) == 1
        assert llm_stub.env_vars_calls[0]["content"] == "SSH to staging.example.com as qa."

    @pytest.mark.asyncio
    async def test_insufficient_submission_skips_extraction(
        self, async_client, db_session, llm_stub
    ):
        sprint = _seed_complete_sprint(db_session)
        llm_stub.check_result = INSUFFICIENT

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "SSH."}
        )

        assert resp.status_code == 200
        assert resp.json()["env_vars"] is None
        assert llm_stub.env_vars_calls == []

    @pytest.mark.asyncio
    async def test_later_insufficient_submission_clears_prior_vars(
        self, async_client, db_session, llm_stub
    ):
        sprint = _seed_complete_sprint(db_session)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "SSH to staging."}
        )
        assert resp.json()["env_vars"] == {"BASE_URL": "https://staging.example.com"}

        llm_stub.check_result = INSUFFICIENT
        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "Vague text."}
        )
        assert resp.status_code == 200
        assert resp.json()["env_vars"] is None

    @pytest.mark.asyncio
    async def test_env_vars_llm_error_persists_nothing(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        llm_stub.env_vars_result = LLMError("provider down")

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "SSH."}
        )

        assert resp.status_code == 502
        get_resp = await async_client.get(f"/api/sprints/{sprint.id}/test-environment")
        assert get_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_answer_extracts_vars_from_rewritten_content(
        self, async_client, db_session, llm_stub
    ):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, clarifying_question="Which host?")

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer",
            json={"answer": "staging.example.com"},
        )

        assert resp.status_code == 200
        assert resp.json()["env_vars"] == {"BASE_URL": "https://staging.example.com"}
        assert llm_stub.env_vars_calls[0]["content"] == "Rewritten access text."

    @pytest.mark.asyncio
    async def test_answer_still_insufficient_skips_extraction(
        self, async_client, db_session, llm_stub
    ):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, clarifying_question="Which host?")
        llm_stub.revise_result = TestEnvironmentResult(
            sufficient=False,
            clarifying_question="And the port?",
            rewritten_content="Rewritten.",
        )

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer", json={"answer": "staging"}
        )

        assert resp.status_code == 200
        assert resp.json()["env_vars"] is None
        assert llm_stub.env_vars_calls == []


# ── PATCH /api/test-environment/{id}/env-vars ─────────────────────────


class TestEditTestEnvironmentVars:
    @pytest.mark.asyncio
    async def test_replaces_vars(self, async_client, db_session):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.READY,
            env_vars_json=json.dumps({"BASE_URL": "https://old.example.com"}),
        )

        resp = await async_client.patch(
            f"/api/test-environment/{row.id}/env-vars",
            json={"variables": {"BASE_URL": "https://correct.example.com", "TOKEN": "abc123"}},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["env_vars"] == {"BASE_URL": "https://correct.example.com", "TOKEN": "abc123"}
        assert data["status"] == "ready"  # untouched

    @pytest.mark.asyncio
    async def test_does_not_touch_content_or_revision_count(self, async_client, db_session):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.READY,
            content="SSH to staging.",
            revision_count=1,
            env_vars_json=json.dumps({"BASE_URL": "https://old.example.com"}),
        )
        original_updated_at = row.updated_at

        resp = await async_client.patch(
            f"/api/test-environment/{row.id}/env-vars",
            json={"variables": {"BASE_URL": "https://new.example.com"}},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "SSH to staging."
        assert data["revision_count"] == 1
        fresh = _reload(db_session, row.id)
        assert fresh.updated_at == original_updated_at

    @pytest.mark.asyncio
    async def test_404_for_missing_row(self, async_client):
        resp = await async_client.patch(
            "/api/test-environment/99999/env-vars", json={"variables": {"A": "b"}}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_422_on_finished_sprint(self, async_client, db_session):
        sprint = _seed_complete_sprint(db_session, active=False)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.READY,
            env_vars_json=json.dumps({"BASE_URL": "x"}),
        )

        resp = await async_client.patch(
            f"/api/test-environment/{row.id}/env-vars", json={"variables": {"A": "b"}}
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_edit_allowed_once_confirmed_and_reopens_for_confirmation(
        self, async_client, db_session
    ):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.CONFIRMED,
            env_vars_json=json.dumps({"BASE_URL": "x"}),
        )

        resp = await async_client.patch(
            f"/api/test-environment/{row.id}/env-vars", json={"variables": {"A": "b"}}
        )

        assert resp.status_code == 200
        # READY, never needs_info: no LLM call ran, so there is no question.
        assert resp.json()["status"] == TestEnvironmentStatus.READY

    @pytest.mark.asyncio
    async def test_edit_removes_plans_but_does_not_touch_updated_at(self, async_client, db_session):
        """`updated_at` means "last LLM check" here — stamping it on a direct
        edit would silently clear a real `requirements_stale` flag."""
        from backend.models.database import TestPlan, TestPlanStatus
        from backend.tests.test_sprints import _seed_test_plan

        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.CONFIRMED,
            env_vars_json=json.dumps({"BASE_URL": "x"}),
        )
        before = row.updated_at
        plan_id = _seed_test_plan(
            db_session, sprint.requirements[0], status=TestPlanStatus.APPROVED
        ).id

        resp = await async_client.patch(
            f"/api/test-environment/{row.id}/env-vars", json={"variables": {"A": "b"}}
        )

        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(TestPlan, plan_id) is None
        assert db_session.get(type(row), row.id).updated_at == before
        assert db_session.get(type(row), row.id).content_revision == 1

    @pytest.mark.asyncio
    async def test_identical_variables_change_nothing(self, async_client, db_session):
        from backend.models.database import TestPlan, TestPlanStatus
        from backend.tests.test_sprints import _seed_test_plan

        sprint = _seed_complete_sprint(db_session)
        variables = {"BASE_URL": "x"}
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.CONFIRMED,
            env_vars_json=json.dumps(variables),
        )
        plan_id = _seed_test_plan(
            db_session, sprint.requirements[0], status=TestPlanStatus.APPROVED
        ).id

        resp = await async_client.patch(
            f"/api/test-environment/{row.id}/env-vars", json={"variables": variables}
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == TestEnvironmentStatus.CONFIRMED
        db_session.expire_all()
        assert db_session.get(TestPlan, plan_id) is not None

    @pytest.mark.asyncio
    async def test_422_on_empty_variables(self, async_client, db_session):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.READY,
            env_vars_json=json.dumps({"BASE_URL": "x"}),
        )

        resp = await async_client.patch(
            f"/api/test-environment/{row.id}/env-vars", json={"variables": {}}
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_422_on_blank_key_or_value(self, async_client, db_session):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.READY,
            env_vars_json=json.dumps({"BASE_URL": "x"}),
        )

        resp = await async_client.patch(
            f"/api/test-environment/{row.id}/env-vars", json={"variables": {"  ": "value"}}
        )
        assert resp.status_code == 422

        resp = await async_client.patch(
            f"/api/test-environment/{row.id}/env-vars", json={"variables": {"BASE_URL": "  "}}
        )
        assert resp.status_code == 422


# ── POST /api/test-environment/{id}/confirm ──────────────────────────


class TestConfirmTestEnvironment:
    @pytest.mark.asyncio
    async def test_ready_to_confirmed(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.READY,
            env_vars_json=json.dumps({"BASE_URL": "https://staging.example.com"}),
        )

        resp = await async_client.post(f"/api/test-environment/{row.id}/confirm")

        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_422_when_env_vars_never_extracted(self, async_client, db_session, llm_stub):
        # Should be unreachable via normal flow (READY implies a sufficient
        # check already populated env_vars_json) — defensive guard only.
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.READY)

        resp = await async_client.post(f"/api/test-environment/{row.id}/confirm")

        assert resp.status_code == 422
        assert "have not been extracted" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_404_for_missing_row(self, async_client):
        resp = await async_client.post("/api/test-environment/99999/confirm")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status", [TestEnvironmentStatus.NEEDS_INFO, TestEnvironmentStatus.CONFIRMED]
    )
    async def test_422_when_not_ready(self, async_client, db_session, status):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, status=status)

        resp = await async_client.post(f"/api/test-environment/{row.id}/confirm")

        assert resp.status_code == 422
        assert "judged sufficient" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_422_on_finished_sprint(self, async_client, db_session):
        sprint = _seed_complete_sprint(db_session, active=False)
        row = _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.READY)

        resp = await async_client.post(f"/api/test-environment/{row.id}/confirm")

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_422_with_non_confirmed_requirement(self, async_client, db_session):
        sprint = _seed_complete_sprint(db_session)
        row = _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.READY)
        _seed_requirement(db_session, sprint, status=RequirementStatus.READY, name="Search")

        resp = await async_client.post(f"/api/test-environment/{row.id}/confirm")

        assert resp.status_code == 422
        assert "requirements must be confirmed" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_422_when_requirements_stale(self, async_client, db_session):
        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED, updated_at=LATER)
        row = _seed_test_env(
            db_session, sprint, status=TestEnvironmentStatus.READY, updated_at=EARLIER
        )

        resp = await async_client.post(f"/api/test-environment/{row.id}/confirm")

        assert resp.status_code == 422
        assert "re-check" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_recheck_clears_staleness(self, async_client, db_session, llm_stub):
        sprint = _seed_sprint(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED, updated_at=LATER)
        row = _seed_test_env(
            db_session, sprint, status=TestEnvironmentStatus.READY, updated_at=EARLIER
        )

        # Fresh re-POST bumps the row's updated_at past the requirement's.
        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": row.content}
        )
        assert resp.status_code == 200

        resp = await async_client.post(f"/api/test-environment/{row.id}/confirm")
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_deleting_confirmed_requirement_does_not_trip_staleness(
        self, async_client, db_session
    ):
        sprint = _seed_sprint(db_session)
        _seed_requirement(
            db_session, sprint, status=RequirementStatus.CONFIRMED, updated_at=EARLIER
        )
        doomed = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.CONFIRMED,
            name="Search",
            updated_at=EARLIER,
        )
        row = _seed_test_env(
            db_session,
            sprint,
            status=TestEnvironmentStatus.READY,
            updated_at=LATER,
            env_vars_json=json.dumps({"BASE_URL": "https://staging.example.com"}),
        )

        del_resp = await async_client.delete(f"/api/requirements/{doomed.id}")
        assert del_resp.status_code == 204

        resp = await async_client.post(f"/api/test-environment/{row.id}/confirm")
        assert resp.status_code == 200


class TestVariableComparisonIgnoresKeyOrder:
    """`env_vars_json` is compared as decoded values, not as JSON text.

    `json.dumps` preserves whatever order the model emitted, so comparing
    the serialized form makes identical variables read as a change and
    deletes every plan in the sprint.

    Exercised through **answer**, not submit: a resubmission of unchanged
    text no longer re-extracts at all (`_resolve_env_vars_json`), so the
    comparison is unreachable there. Answering runs a fresh extraction on
    the rewritten text, and a rewrite that comes back byte-identical — the
    answer clarified nothing the text did not already say — lands on
    exactly this comparison.
    """

    def _seed_answerable(self, db_session, env_vars: dict, content="SSH into staging."):
        row = _seed_test_env(
            db_session,
            _seed_complete_sprint(db_session),
            content=content,
            original_content=content,
            status=TestEnvironmentStatus.NEEDS_INFO,
            clarifying_question="Which host?",
            env_vars_json=json.dumps(env_vars),
        )
        return row

    @pytest.mark.asyncio
    async def test_reordered_variables_are_not_a_change(self, async_client, db_session, llm_stub):
        from backend.models.database import TestPlan, TestPlanStatus
        from backend.tests.test_sprints import _seed_test_plan

        row = self._seed_answerable(db_session, {"A": "1", "B": "2"})
        plan_id = _seed_test_plan(
            db_session, row.sprint.requirements[0], status=TestPlanStatus.APPROVED
        ).id
        llm_stub.revise_result = TestEnvironmentResult(
            sufficient=True, clarifying_question=None, rewritten_content=row.content
        )
        # Same pairs, opposite order.
        llm_stub.env_vars_result = EnvVarsResult(variables={"B": "2", "A": "1"})

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer", json={"answer": "staging.example.com"}
        )

        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(TestEnvironmentAccess, row.id).content_revision == 0
        assert db_session.get(TestPlan, plan_id) is not None

    @pytest.mark.asyncio
    async def test_a_genuine_variable_change_still_cascades(
        self, async_client, db_session, llm_stub
    ):
        from backend.models.database import TestPlan, TestPlanStatus
        from backend.tests.test_sprints import _seed_test_plan

        row = self._seed_answerable(db_session, {"A": "1"})
        plan_id = _seed_test_plan(
            db_session, row.sprint.requirements[0], status=TestPlanStatus.APPROVED
        ).id
        llm_stub.revise_result = TestEnvironmentResult(
            sufficient=True, clarifying_question=None, rewritten_content=row.content
        )
        llm_stub.env_vars_result = EnvVarsResult(variables={"A": "CHANGED"})

        resp = await async_client.post(
            f"/api/test-environment/{row.id}/answer", json={"answer": "the value is different"}
        )

        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(TestEnvironmentAccess, row.id).content_revision == 1
        assert db_session.get(TestPlan, plan_id) is None
