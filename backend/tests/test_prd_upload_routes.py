"""Tests for POST /api/sprints/{id}/requirements/from-prd."""

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import select

from backend.models.database import (
    Repo,
    Requirement,
    RequirementStatus,
    Sprint,
    TestEnvironmentAccess,
    TestEnvironmentStatus,
)
from backend.services.llm import LLMError, PrdRequirementItem, PrdSplitResult
from backend.services.storage import StorageService

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ── Seed helpers ──────────────────────────────────────────────────────


def _seed_sprint(db_session, active: bool = True) -> Sprint:
    repo = Repo(github_link="https://github.com/owner/repo", name="owner/repo")
    db_session.add(repo)
    db_session.commit()
    sprint = Sprint(
        name="Sprint",
        repo_id=repo.id,
        active=active,
        directory=f"dir-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(sprint)
    db_session.commit()
    db_session.refresh(sprint)
    return sprint


def _seed_requirement(
    db_session,
    sprint: Sprint,
    status: RequirementStatus = RequirementStatus.PENDING,
    **kwargs,
) -> Requirement:
    requirement = Requirement(
        sprint_id=sprint.id,
        name=kwargs.pop("name", "Login"),
        description=kwargs.pop("description", "Users can log in."),
        original_description=kwargs.pop("original_description", "Users can log in."),
        status=status,
        **kwargs,
    )
    db_session.add(requirement)
    db_session.commit()
    db_session.refresh(requirement)
    return requirement


def _lock_requirements(db_session, sprint: Sprint) -> None:
    db_session.add(
        TestEnvironmentAccess(
            sprint_id=sprint.id,
            content="SSH to staging.",
            original_content="SSH to staging.",
            status=TestEnvironmentStatus.CONFIRMED,
        )
    )
    db_session.commit()


def _requirement_ids(db_session, sprint_id: int) -> set[int]:
    return set(
        db_session.exec(select(Requirement.id).where(Requirement.sprint_id == sprint_id)).all()
    )


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_readme(monkeypatch):
    """Force resolve_readme → None so no test touches GitHub."""
    import backend.routes.requirements as requirements_module

    async def _none(sprint):
        return None

    monkeypatch.setattr(requirements_module, "resolve_readme", _none)


@pytest.fixture
def split_stub(monkeypatch):
    """Replace llm.split_prd with a controllable recording stub."""
    import backend.services.llm as llm_module

    state = SimpleNamespace(
        result=PrdSplitResult(
            requirements=[
                PrdRequirementItem(name="Login", description="Users can log in."),
                PrdRequirementItem(name="Upload", description="Users can upload files."),
            ]
        ),
        error=None,
        calls=[],
    )

    def _fake(prd_text, readme, file_tree):
        state.calls.append((prd_text, readme, file_tree))
        if state.error is not None:
            raise state.error
        return state.result

    monkeypatch.setattr(llm_module, "split_prd", _fake)
    return state


class _StubQueueService:
    def __init__(self):
        self.enqueued: list[int] = []

    def enqueue_analysis(self, requirement_id: int):
        self.enqueued.append(requirement_id)
        return SimpleNamespace(id=f"job-{requirement_id}")


@pytest.fixture
def stub_queue(monkeypatch):
    stub = _StubQueueService()
    import backend.routes.requirements as requirements_module

    monkeypatch.setattr(requirements_module, "get_queue_service", lambda: stub)
    return stub


def _md_upload(content: bytes = b"# PRD\n\nUsers log in and upload files."):
    return {"prd_file": ("prd.md", content, "text/markdown")}


async def _upload(client, sprint_id: int, files=None):
    return await client.post(f"/api/sprints/{sprint_id}/requirements/from-prd", files=files)


# ── Happy path ────────────────────────────────────────────────────────


class TestPrdUploadHappyPath:
    @pytest.mark.asyncio
    async def test_creates_pending_prd_rows(self, async_client, db_session, split_stub):
        sprint = _seed_sprint(db_session)

        resp = await _upload(async_client, sprint.id, _md_upload())

        assert resp.status_code == 201
        body = resp.json()
        assert [item["name"] for item in body] == ["Login", "Upload"]
        assert all(item["from_prd"] is True for item in body)
        assert all(item["status"] == "pending" for item in body)
        assert all(item["description"] == item["original_description"] for item in body)
        # the extracted markdown text reached the LLM verbatim
        assert split_stub.calls[0][0] == "# PRD\n\nUsers log in and upload files."

    @pytest.mark.asyncio
    async def test_enqueues_each_row(self, async_client, db_session, split_stub, stub_queue):
        sprint = _seed_sprint(db_session)

        resp = await _upload(async_client, sprint.id, _md_upload())

        assert resp.status_code == 201
        assert stub_queue.enqueued == [item["id"] for item in resp.json()]

    @pytest.mark.asyncio
    async def test_pdf_upload(self, async_client, db_session, split_stub):
        sprint = _seed_sprint(db_session)
        content = (_FIXTURES / "sample_prd.pdf").read_bytes()

        resp = await _upload(
            async_client,
            sprint.id,
            {"prd_file": ("prd.pdf", content, "application/pdf")},
        )

        assert resp.status_code == 201
        assert "upload a PRD document" in split_stub.calls[0][0]


# ── Overwrite semantics ───────────────────────────────────────────────


class TestPrdUploadOverwrite:
    @pytest.mark.asyncio
    async def test_replaces_prior_prd_rows_only(self, async_client, db_session, split_stub):
        sprint = _seed_sprint(db_session)
        manual = _seed_requirement(db_session, sprint, name="Manual")
        _seed_requirement(db_session, sprint, name="Old PRD", from_prd=True)
        _seed_requirement(
            db_session,
            sprint,
            status=RequirementStatus.CONFIRMED,
            name="Old confirmed PRD",
            from_prd=True,
        )
        manual_id = manual.id

        resp = await _upload(async_client, sprint.id, _md_upload())

        assert resp.status_code == 201
        # Assert by name — SQLite recycles the deleted rows' ids for the
        # new inserts, so id-based absence checks would false-positive.
        remaining_names = set(
            db_session.exec(
                select(Requirement.name).where(Requirement.sprint_id == sprint.id)
            ).all()
        )
        assert remaining_names == {"Manual", "Login", "Upload"}
        assert manual_id in _requirement_ids(db_session, sprint.id)

    @pytest.mark.asyncio
    async def test_llm_failure_leaves_existing_rows(self, async_client, db_session, split_stub):
        sprint = _seed_sprint(db_session)
        old_prd = _seed_requirement(db_session, sprint, name="Old PRD", from_prd=True)
        split_stub.error = LLMError("provider exploded")

        resp = await _upload(async_client, sprint.id, _md_upload())

        assert resp.status_code == 502
        assert "provider exploded" in resp.json()["detail"]
        assert old_prd.id in _requirement_ids(db_session, sprint.id)

    @pytest.mark.asyncio
    async def test_empty_split_leaves_existing_rows(self, async_client, db_session, split_stub):
        sprint = _seed_sprint(db_session)
        old_prd = _seed_requirement(db_session, sprint, name="Old PRD", from_prd=True)
        split_stub.result = PrdSplitResult(requirements=[])

        resp = await _upload(async_client, sprint.id, _md_upload())

        assert resp.status_code == 422
        assert "No requirements could be found" in resp.json()["detail"]
        assert old_prd.id in _requirement_ids(db_session, sprint.id)

    @pytest.mark.asyncio
    async def test_over_requirement_cap_leaves_existing_rows(
        self, async_client, db_session, split_stub, monkeypatch
    ):
        import backend.routes.requirements as requirements_module

        monkeypatch.setattr(requirements_module, "MAX_PRD_REQUIREMENTS", 1)
        sprint = _seed_sprint(db_session)
        old_prd = _seed_requirement(db_session, sprint, name="Old PRD", from_prd=True)

        resp = await _upload(async_client, sprint.id, _md_upload())

        assert resp.status_code == 422
        assert "produced 2 requirements" in resp.json()["detail"]
        assert old_prd.id in _requirement_ids(db_session, sprint.id)


# ── Validation failures (before any LLM call) ─────────────────────────


class TestPrdUploadValidation:
    @pytest.mark.asyncio
    async def test_unsupported_extension(self, async_client, db_session, split_stub):
        sprint = _seed_sprint(db_session)

        resp = await _upload(
            async_client, sprint.id, {"prd_file": ("prd.rtf", b"x", "application/rtf")}
        )

        assert resp.status_code == 422
        assert "Unsupported PRD file type" in resp.json()["detail"]
        assert split_stub.calls == []

    @pytest.mark.asyncio
    async def test_corrupt_pdf(self, async_client, db_session, split_stub):
        sprint = _seed_sprint(db_session)

        resp = await _upload(
            async_client,
            sprint.id,
            {"prd_file": ("prd.pdf", b"%PDF- not a pdf", "application/pdf")},
        )

        assert resp.status_code == 422
        assert "Could not read the PDF" in resp.json()["detail"]
        assert split_stub.calls == []

    @pytest.mark.asyncio
    async def test_over_char_cap(self, async_client, db_session, split_stub, monkeypatch):
        import backend.routes.requirements as requirements_module

        monkeypatch.setattr(requirements_module, "PRD_MAX_CHARS", 10)
        sprint = _seed_sprint(db_session)

        resp = await _upload(async_client, sprint.id, _md_upload(b"a longer PRD document"))

        assert resp.status_code == 422
        assert "the limit is 10" in resp.json()["detail"]
        assert split_stub.calls == []

    @pytest.mark.asyncio
    async def test_over_raw_size_cap(self, async_client, db_session, split_stub, monkeypatch):
        import backend.routes.requirements as requirements_module

        monkeypatch.setattr(requirements_module, "MAX_UPLOAD_SIZE_MB", 0)
        sprint = _seed_sprint(db_session)

        resp = await _upload(async_client, sprint.id, _md_upload())

        assert resp.status_code == 422
        assert "upload limit" in resp.json()["detail"]
        assert split_stub.calls == []

    @pytest.mark.asyncio
    async def test_finished_sprint(self, async_client, db_session, split_stub):
        sprint = _seed_sprint(db_session, active=False)

        resp = await _upload(async_client, sprint.id, _md_upload())

        assert resp.status_code == 422
        assert split_stub.calls == []

    @pytest.mark.asyncio
    async def test_locked_requirements(self, async_client, db_session, split_stub):
        sprint = _seed_sprint(db_session)
        _lock_requirements(db_session, sprint)

        resp = await _upload(async_client, sprint.id, _md_upload())

        assert resp.status_code == 422
        assert "locked" in resp.json()["detail"]
        assert split_stub.calls == []

    @pytest.mark.asyncio
    async def test_unknown_sprint(self, async_client, split_stub):
        resp = await _upload(async_client, 9999, _md_upload())
        assert resp.status_code == 404


# ── PRD storage ───────────────────────────────────────────────────────


class TestStorePrd:
    def test_offline_writes_original_bytes(self, monkeypatch, tmp_path):
        monkeypatch.setattr("backend.services.storage.STORE_OFFLINE", True)
        monkeypatch.setattr("backend.services.storage.STORAGE_LOCATION", str(tmp_path))
        content = b"%PDF binary \x00 bytes"

        path = StorageService().store_prd(content, "sprint-dir", "My PRD.PDF")

        assert path == str(tmp_path / "sprint-dir" / "PRD.pdf")
        assert Path(path).read_bytes() == content

    def test_online_is_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr("backend.services.storage.STORE_OFFLINE", False)
        monkeypatch.setattr("backend.services.storage.STORAGE_LOCATION", str(tmp_path))

        assert StorageService().store_prd(b"x", "sprint-dir", "prd.md") is None
        assert not (tmp_path / "sprint-dir").exists()
