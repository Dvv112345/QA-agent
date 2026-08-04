"""Tests for backend/tasks/generate_test_plan.py — the task as a plain function.

The conftest engine monkeypatch makes ``new_session()`` hit the same
in-memory SQLite database as ``db_session``; ``services.llm`` plan functions
and ``github_utils.fetch_file`` are monkeypatched — no Redis, no network.
"""

import json

import pytest
from sqlmodel import select

from backend.config import MAX_AUTO_RETRIES
from backend.models.database import (
    SPRINT_FINISHED_ERROR,
    RequirementStatus,
    TestCase,
    TestPlan,
    TestPlanStatus,
)
from backend.services.llm import LLMError, TestCaseResult, TestPlanResult
from backend.tasks.generate_test_plan import generate_test_plan_task
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import _seed_test_case, _seed_test_env, _seed_test_plan

RESULT = TestPlanResult(
    complexity="medium",
    summary="Covers the login flows.",
    cases=[
        TestCaseResult(
            title="Valid login",
            preconditions="A registered user exists.",
            steps=["Open the login page", "Enter valid credentials"],
            expected_result="User lands on the dashboard.",
            case_type="functional",
            priority="high",
        ),
        TestCaseResult(
            title="Invalid login",
            preconditions=None,
            steps=["Open the login page", "Enter a wrong password"],
            expected_result="An error message is shown.",
            case_type="negative",
            priority="medium",
        ),
    ],
)


@pytest.fixture(autouse=True)
def _isolate_readme_resolution(monkeypatch):
    """Keep README resolution deterministic: no disk reads, no GitHub calls."""
    import backend.utils.readme_utils as readme_utils

    async def _no_readme(*args, **kwargs):
        return None

    monkeypatch.setattr(readme_utils, "STORE_OFFLINE", False)
    monkeypatch.setattr(readme_utils, "download_readme", _no_readme)


@pytest.fixture
def llm_stub(monkeypatch):
    """Replace both plan LLM entry points with a recording stub.

    ``stub.result`` may be a ``TestPlanResult`` or an exception to raise.
    The task must call both functions with keyword arguments only.
    """

    class _Stub:
        def __init__(self):
            self.result = RESULT
            self.generate_calls: list[dict] = []
            self.revise_calls: list[dict] = []

        def _resolve(self):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

        def generate_test_plan(self, **kwargs):
            self.generate_calls.append(kwargs)
            return self._resolve()

        def revise_test_plan(self, **kwargs):
            self.revise_calls.append(kwargs)
            return self._resolve()

    stub = _Stub()
    import backend.services.llm as llm_module

    monkeypatch.setattr(llm_module, "generate_test_plan", stub.generate_test_plan)
    monkeypatch.setattr(llm_module, "revise_test_plan", stub.revise_test_plan)
    return stub


@pytest.fixture
def fetch_stub(monkeypatch):
    """Replace ``github_utils.fetch_file`` with a recording async stub."""

    class _Fetch:
        def __init__(self):
            self.result = "file contents"
            self.calls: list[dict] = []

    stub = _Fetch()

    async def _fetch(owner, repo, path, token=None, ref=None):
        stub.calls.append({"owner": owner, "repo": repo, "path": path, "token": token})
        if isinstance(stub.result, Exception):
            raise stub.result
        return stub.result

    import backend.utils.github_utils as github_utils

    monkeypatch.setattr(github_utils, "fetch_file", _fetch)
    return stub


def _seed_setup(db_session, *, file_tree="src/app.py\nsrc/db.py", active=True):
    """Sprint (repo with file tree + confirmed test env) + requirement + plan."""
    sprint = _seed_sprint(db_session, active=active)
    if file_tree is not None:
        sprint.repo.file_tree = file_tree
        db_session.add(sprint.repo)
        db_session.commit()
    _seed_test_env(db_session, sprint, content="SSH to staging as qa.")
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    plan = _seed_test_plan(db_session, requirement)
    return sprint, requirement, plan


def _reload(db_session, plan_id) -> TestPlan:
    db_session.expire_all()
    return db_session.get(TestPlan, plan_id)


class TestInitialGeneration:
    def test_pending_to_draft_with_cases(self, db_session, llm_stub, fetch_stub):
        _, _, plan = _seed_setup(db_session)

        generate_test_plan_task(plan.id)

        row = _reload(db_session, plan.id)
        assert row.status == TestPlanStatus.DRAFT
        assert row.complexity == "medium"
        assert row.summary == "Covers the login flows."
        assert row.retry_count == 0
        assert row.last_heartbeat is None
        assert row.error is None
        assert [(c.position, c.title) for c in row.cases] == [
            (0, "Valid login"),
            (1, "Invalid login"),
        ]
        assert row.cases[0].steps == "Open the login page\nEnter valid credentials"
        assert row.cases[0].preconditions == "A registered user exists."
        assert row.cases[1].priority == "medium"
        assert llm_stub.revise_calls == []

    def test_context_passed_to_llm(self, db_session, llm_stub, fetch_stub):
        sprint, requirement, plan = _seed_setup(db_session)
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED, name="Search")
        _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED, name="Checkout")

        generate_test_plan_task(plan.id)

        call = llm_stub.generate_calls[0]
        assert call["name"] == requirement.name
        assert call["description"] == requirement.description
        assert call["sibling_names"] == ["Search", "Checkout"]
        assert call["test_env_content"] == "SSH to staging as qa."
        assert call["file_tree"] == "src/app.py\nsrc/db.py"
        assert call["readme"] is None

    def test_generating_row_is_also_processed(self, db_session, llm_stub, fetch_stub):
        """Idempotent re-run of a row a crashed worker left in generating."""
        _, _, plan = _seed_setup(db_session)
        plan.status = TestPlanStatus.GENERATING
        db_session.add(plan)
        db_session.commit()

        generate_test_plan_task(plan.id)

        assert _reload(db_session, plan.id).status == TestPlanStatus.DRAFT


class TestRevision:
    def test_feedback_revision_replaces_cases(self, db_session, llm_stub, fetch_stub):
        _, _, plan = _seed_setup(db_session)
        plan.complexity = "low"
        plan.summary = "Old summary"
        plan.pending_feedback = "Add negative cases."
        db_session.add(plan)
        db_session.commit()
        old_case = _seed_test_case(db_session, plan, position=0, title="Old case")
        old_case_id = old_case.id

        generate_test_plan_task(plan.id)

        row = _reload(db_session, plan.id)
        assert row.status == TestPlanStatus.DRAFT
        assert row.revision_count == 1
        assert row.pending_feedback is None
        assert row.complexity == "medium"
        assert row.summary == "Covers the login flows."
        # The superseded case is archived, not deleted: a run that already
        # executed it reads its title and steps off this row. It drops out
        # of `plan.cases` while staying in the table.
        old_case_row = db_session.exec(select(TestCase).where(TestCase.id == old_case_id)).one()
        assert old_case_row.archived is True
        assert [c.title for c in row.cases] == ["Valid login", "Invalid login"]
        assert old_case_id not in {c.id for c in row.cases}
        # A rewritten case set makes any run against the old one outdated.
        assert row.content_revision == 1
        assert llm_stub.generate_calls == []

    def test_current_plan_json_and_feedback_passed(self, db_session, llm_stub, fetch_stub):
        _, _, plan = _seed_setup(db_session)
        plan.complexity = "low"
        plan.summary = "Old summary"
        plan.pending_feedback = "Add negative cases."
        db_session.add(plan)
        db_session.commit()
        _seed_test_case(db_session, plan, position=0, title="Old case", steps="Step one\nStep two")

        generate_test_plan_task(plan.id)

        call = llm_stub.revise_calls[0]
        assert call["feedback"] == "Add negative cases."
        current = json.loads(call["current_plan_json"])
        assert current["complexity"] == "low"
        assert current["summary"] == "Old summary"
        assert current["cases"][0]["title"] == "Old case"
        # steps serialized back to a list for the LLM (mirrors TestPlanResult shape)
        assert current["cases"][0]["steps"] == ["Step one", "Step two"]

    def test_direct_edits_never_bump_revision_count(self, db_session, llm_stub, fetch_stub):
        """Only the feedback loop increments revision_count — an initial
        generation (no pending_feedback) must leave it untouched."""
        _, _, plan = _seed_setup(db_session)

        generate_test_plan_task(plan.id)

        assert _reload(db_session, plan.id).revision_count == 0


class TestIdempotencyGuards:
    @pytest.mark.parametrize(
        "status",
        [TestPlanStatus.DRAFT, TestPlanStatus.APPROVED, TestPlanStatus.FAILED],
    )
    def test_skips_settled_rows(self, db_session, llm_stub, fetch_stub, status):
        _, _, plan = _seed_setup(db_session)
        plan.status = status
        db_session.add(plan)
        db_session.commit()

        generate_test_plan_task(plan.id)

        assert _reload(db_session, plan.id).status == status
        assert llm_stub.generate_calls == []
        assert llm_stub.revise_calls == []

    def test_missing_row_is_noop(self, db_session, llm_stub, fetch_stub):
        generate_test_plan_task(99999)
        assert llm_stub.generate_calls == []


class TestFinishedSprintGuards:
    def test_inactive_sprint_marks_plan_failed(self, db_session, llm_stub, fetch_stub):
        _, _, plan = _seed_setup(db_session, active=False)
        plan.pending_feedback = "stale feedback"
        db_session.add(plan)
        db_session.commit()

        generate_test_plan_task(plan.id)

        row = _reload(db_session, plan.id)
        assert row.status == TestPlanStatus.FAILED
        assert row.error == SPRINT_FINISHED_ERROR
        assert row.pending_feedback is None
        assert row.last_heartbeat is None
        assert llm_stub.generate_calls == []

    def test_archived_requirement_marks_plan_failed(self, db_session, llm_stub, fetch_stub):
        """A deleted requirement gets the same disposition as a vanished one —
        never generate a plan for something the user removed."""
        _, requirement, plan = _seed_setup(db_session)
        requirement.archived = True
        db_session.add(requirement)
        db_session.commit()

        generate_test_plan_task(plan.id)

        row = _reload(db_session, plan.id)
        assert row.status == TestPlanStatus.FAILED
        assert llm_stub.generate_calls == []

    def test_discards_result_when_status_changed_mid_run(self, db_session, fetch_stub, monkeypatch):
        """A plan failed/reset while the LLM loop was in flight keeps that state."""
        import backend.services.llm as llm_module
        from backend.database import new_session

        _, _, plan = _seed_setup(db_session)

        def _flip_status_then_answer(**kwargs):
            # Simulates the finish-sprint sweep landing mid-LLM-loop.
            with new_session() as other:
                row = other.get(TestPlan, plan.id)
                row.status = TestPlanStatus.FAILED
                row.error = SPRINT_FINISHED_ERROR
                other.add(row)
                other.commit()
            return RESULT

        monkeypatch.setattr(llm_module, "generate_test_plan", _flip_status_then_answer)

        generate_test_plan_task(plan.id)

        row = _reload(db_session, plan.id)
        assert row.status == TestPlanStatus.FAILED
        assert row.error == SPRINT_FINISHED_ERROR
        assert row.cases == []


class TestFailureHandling:
    def test_llm_error_returns_plan_to_pending(self, db_session, llm_stub, fetch_stub):
        _, _, plan = _seed_setup(db_session)
        llm_stub.result = LLMError("boom")

        generate_test_plan_task(plan.id)

        row = _reload(db_session, plan.id)
        assert row.status == TestPlanStatus.PENDING
        assert row.retry_count == 1
        assert row.error is None
        assert row.last_heartbeat is None

    def test_retries_exhausted_marks_failed(self, db_session, llm_stub, fetch_stub):
        _, _, plan = _seed_setup(db_session)
        plan.retry_count = MAX_AUTO_RETRIES - 1
        db_session.add(plan)
        db_session.commit()
        llm_stub.result = LLMError("boom " * 200)

        generate_test_plan_task(plan.id)

        row = _reload(db_session, plan.id)
        assert row.status == TestPlanStatus.FAILED
        assert row.retry_count == MAX_AUTO_RETRIES
        assert row.error is not None
        assert len(row.error) <= 300

    def test_failed_revision_keeps_pending_feedback(self, db_session, llm_stub, fetch_stub):
        """The feedback survives a transient failure so Restart resumes it."""
        _, _, plan = _seed_setup(db_session)
        plan.pending_feedback = "Add negative cases."
        db_session.add(plan)
        db_session.commit()
        llm_stub.result = LLMError("boom")

        generate_test_plan_task(plan.id)

        row = _reload(db_session, plan.id)
        assert row.status == TestPlanStatus.PENDING
        assert row.pending_feedback == "Add negative cases."


class TestPlanningIsCodeBlind:
    """Planning never reads the repository.

    A plan defines what "correct" means for a requirement, so reading the
    implementation is exactly where that judgment drifts into describing
    what the code already does. The interface details a script needs are
    resolved later, by ``generate_test_script``, which does have repo
    access. These tests exist so that separation cannot be undone quietly.
    """

    def test_generation_never_fetches_repo_files(self, db_session, llm_stub, fetch_stub):
        _, _, plan = _seed_setup(db_session)

        generate_test_plan_task(plan.id)

        assert fetch_stub.calls == []
        assert _reload(db_session, plan.id).status == TestPlanStatus.DRAFT

    def test_revision_never_fetches_repo_files(self, db_session, llm_stub, fetch_stub):
        _, _, plan = _seed_setup(db_session)
        plan.pending_feedback = "Add negative cases."
        db_session.add(plan)
        db_session.commit()

        generate_test_plan_task(plan.id)

        assert fetch_stub.calls == []

    def test_no_read_file_or_on_round_is_passed(self, db_session, llm_stub, fetch_stub):
        """Passing read_file=None would leave re-enabling it one argument
        away; the parameter is gone from the signature entirely."""
        _, _, plan = _seed_setup(db_session)

        generate_test_plan_task(plan.id)

        call = llm_stub.generate_calls[0]
        assert "read_file" not in call
        assert "on_round" not in call
        # The file tree still reaches the prompt — structure without
        # behaviour is exactly the grounding a plan should have.
        assert call["file_tree"] == "src/app.py\nsrc/db.py"

    def test_heartbeat_is_stamped_before_the_call(self, db_session, fetch_stub, monkeypatch):
        """With no per-round callback, the pre-call stamp is what keeps the
        reconciler from sweeping a live plan job."""
        import backend.services.llm as llm_module

        seen = {}

        def _generate(**kwargs):
            seen["heartbeat"] = _reload(db_session, plan.id).last_heartbeat
            return RESULT

        _, _, plan = _seed_setup(db_session)
        monkeypatch.setattr(llm_module, "generate_test_plan", _generate)

        generate_test_plan_task(plan.id)

        assert seen["heartbeat"] is not None
