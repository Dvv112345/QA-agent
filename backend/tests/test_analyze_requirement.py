"""Tests for backend/tasks/analyze_requirement.py — the task as a plain function.

The conftest engine monkeypatch makes ``new_session()`` hit the same
in-memory SQLite database as ``db_session``, and ``services.llm`` functions
are monkeypatched — no Redis, no network.
"""

import pytest

from backend.models.database import Requirement, RequirementStatus
from backend.services.llm import ClarityResult, LLMError
from backend.tasks.analyze_requirement import analyze_requirement_task
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint

CLEAR = ClarityResult(clear=True, clarifying_question=None)
UNCLEAR = ClarityResult(clear=False, clarifying_question="Which users?")


@pytest.fixture(autouse=True)
def _isolate_readme_resolution(monkeypatch):
    """Keep README resolution deterministic: no disk reads, no GitHub calls."""
    import backend.tasks.analyze_requirement as task_module

    async def _no_readme(*args, **kwargs):
        return None

    monkeypatch.setattr(task_module, "STORE_OFFLINE", False)
    monkeypatch.setattr(task_module, "download_readme", _no_readme)


@pytest.fixture
def llm_stub(monkeypatch):
    """Replace both LLM entry points with a recording stub.

    ``stub.result`` may be a ``ClarityResult`` or an exception to raise.
    """

    class _Stub:
        def __init__(self):
            self.result = CLEAR
            self.check_calls: list[dict] = []
            self.revise_calls: list[dict] = []

        def _resolve(self):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

        def check_clarity(self, name, description, readme, file_tree):
            self.check_calls.append(
                {"name": name, "description": description, "readme": readme, "file_tree": file_tree}
            )
            return self._resolve()

        def revise_requirement(self, name, description, question, answer, readme, file_tree):
            self.revise_calls.append(
                {"question": question, "answer": answer, "readme": readme, "file_tree": file_tree}
            )
            return self._resolve()

    stub = _Stub()
    import backend.services.llm as llm_module

    monkeypatch.setattr(llm_module, "check_clarity", stub.check_clarity)
    monkeypatch.setattr(llm_module, "revise_requirement", stub.revise_requirement)
    return stub


def _reload(db_session, requirement_id) -> Requirement:
    db_session.expire_all()
    return db_session.get(Requirement, requirement_id)


class TestInitialCheck:
    def test_pending_to_ready(self, db_session, llm_stub):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint)

        analyze_requirement_task(req.id)

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.READY
        assert row.clarifying_question is None
        assert row.retry_count == 0
        assert row.last_heartbeat is None

    def test_pending_to_needs_clarification(self, db_session, llm_stub):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint)
        llm_stub.result = UNCLEAR

        analyze_requirement_task(req.id)

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.NEEDS_CLARIFICATION
        assert row.clarifying_question == "Which users?"

    def test_file_tree_passed_from_repo(self, db_session, llm_stub):
        sprint = _seed_sprint(db_session)
        sprint.repo.file_tree = "src/app.py\nsrc/db.py"
        db_session.add(sprint.repo)
        db_session.commit()
        req = _seed_requirement(db_session, sprint)

        analyze_requirement_task(req.id)

        assert llm_stub.check_calls[0]["file_tree"] == "src/app.py\nsrc/db.py"


class TestRevisionPath:
    def test_answer_consumed_and_description_rewritten(self, db_session, llm_stub):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.PENDING,
            clarifying_question="Which users?",
            pending_answer="Registered users only.",
        )
        llm_stub.result = ClarityResult(
            clear=True,
            clarifying_question=None,
            rewritten_description="Registered users can log in.",
        )

        analyze_requirement_task(req.id)

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.READY
        assert row.description == "Registered users can log in."
        assert row.revision_count == 1
        assert row.pending_answer is None
        assert llm_stub.revise_calls[0]["question"] == "Which users?"
        assert llm_stub.revise_calls[0]["answer"] == "Registered users only."
        assert llm_stub.check_calls == []


class TestFailureHandling:
    def test_llm_error_returns_row_to_pending(self, db_session, llm_stub):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint)
        llm_stub.result = LLMError("boom")

        analyze_requirement_task(req.id)

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.PENDING
        assert row.retry_count == 1
        assert row.error is None
        assert row.last_heartbeat is None

    def test_retries_exhausted_marks_failed(self, db_session, llm_stub):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, retry_count=2)
        llm_stub.result = LLMError("boom " * 200)

        analyze_requirement_task(req.id)

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.FAILED
        assert row.retry_count == 3
        assert row.error is not None
        assert len(row.error) <= 300


class TestIdempotencyGuards:
    @pytest.mark.parametrize(
        "status",
        [
            RequirementStatus.CONFIRMED,
            RequirementStatus.READY,
            RequirementStatus.NEEDS_CLARIFICATION,
            RequirementStatus.FAILED,
        ],
    )
    def test_skips_non_pending_rows(self, db_session, llm_stub, status):
        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint, status=status)

        analyze_requirement_task(req.id)

        row = _reload(db_session, req.id)
        assert row.status == status
        assert llm_stub.check_calls == []
        assert llm_stub.revise_calls == []

    def test_missing_row_is_noop(self, db_session, llm_stub):
        analyze_requirement_task(99999)
        assert llm_stub.check_calls == []


class TestReadmeResolution:
    def test_reads_stored_readme(self, db_session, llm_stub, monkeypatch, tmp_path):
        import backend.tasks.analyze_requirement as task_module

        monkeypatch.setattr(task_module, "STORE_OFFLINE", True)
        monkeypatch.setattr(task_module, "STORAGE_LOCATION", str(tmp_path))

        sprint = _seed_sprint(db_session)
        readme_dir = tmp_path / sprint.directory
        readme_dir.mkdir()
        (readme_dir / "README.md").write_text("# Stored README", encoding="utf-8")
        req = _seed_requirement(db_session, sprint)

        analyze_requirement_task(req.id)

        assert llm_stub.check_calls[0]["readme"] == "# Stored README"

    def test_download_failure_degrades_to_none(self, db_session, llm_stub, monkeypatch):
        import backend.tasks.analyze_requirement as task_module

        async def _fail(*args, **kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr(task_module, "download_readme", _fail)

        sprint = _seed_sprint(db_session)
        req = _seed_requirement(db_session, sprint)

        analyze_requirement_task(req.id)

        row = _reload(db_session, req.id)
        assert row.status == RequirementStatus.READY
        assert llm_stub.check_calls[0]["readme"] is None
