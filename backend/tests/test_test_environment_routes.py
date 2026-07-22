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
    async def test_422_once_confirmed(self, async_client, db_session, llm_stub):
        sprint = _seed_complete_sprint(db_session)
        _seed_test_env(db_session, sprint, status=TestEnvironmentStatus.CONFIRMED)

        resp = await async_client.post(
            f"/api/sprints/{sprint.id}/test-environment", json={"content": "New text."}
        )

        assert resp.status_code == 422
        assert "confirmed" in resp.json()["detail"]

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
    async def test_422_once_confirmed(self, async_client, db_session):
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
        assert resp.status_code == 422
        assert "locked" in resp.json()["detail"].lower()

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
