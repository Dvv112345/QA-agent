"""Config routes for CI/CD export — verification, token resolution, nothing-on-failure."""

import pytest
from pytest_httpx import HTTPXMock
from sqlmodel import select

from backend.models.database import CicdConfig, CicdProvider, Repo
from backend.tests.test_requirement_routes import _seed_sprint
from backend.utils.crypto import decrypt_token, encrypt_token

# `_seed_sprint` registers this repository, so the export destination — which
# is always the sprint's own repo — resolves to owner/repo.
_REPO_API = "https://api.github.com/repos/owner/repo"


def _sprint_id(db_session, active: bool = True) -> int:
    """Seed a sprint straight into the database and return its id.

    Deliberately not through `POST /api/sprints`: that path fetches repo
    metadata, a README and a file tree, so three unrelated GitHub mocks
    would sit between every test and the one request it is actually about.
    """
    return _seed_sprint(db_session, active=active).id


def _push(granted: bool) -> dict:
    return {"full_name": "owner/repo", "permissions": {"pull": True, "push": granted}}


# ── GET ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_null_when_no_config_exists(async_client, db_session):
    sprint_id = _sprint_id(db_session)

    resp = await async_client.get(f"/api/sprints/{sprint_id}/cicd-config")

    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.asyncio
async def test_get_404s_for_an_unknown_sprint(async_client):
    assert (await async_client.get("/api/sprints/9999/cicd-config")).status_code == 404


# ── PUT: verification ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_with_a_pushable_token_persists_and_hides_the_token(
    async_client, db_session, httpx_mock: HTTPXMock
):
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(url=_REPO_API, json=_push(True))

    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={
            "provider": "github_actions",
            "access_token": "ghp_write",
            "ci_environment_hint": "self-hosted runner",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "github_actions"
    assert body["ci_environment_hint"] == "self-hosted runner"
    assert body["verified_at"]
    assert "access_token" not in body
    assert "ghp_write" not in resp.text

    stored = db_session.exec(select(CicdConfig)).one()
    assert decrypt_token(stored.access_token) == "ghp_write"


@pytest.mark.asyncio
async def test_put_with_a_read_only_token_422s_and_persists_nothing(
    async_client, db_session, httpx_mock: HTTPXMock
):
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(url=_REPO_API, json=_push(False))

    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "access_token": "ghp_read"},
    )

    assert resp.status_code == 422
    assert "cannot push" in resp.json()["detail"]
    assert db_session.exec(select(CicdConfig)).all() == []


@pytest.mark.asyncio
async def test_put_422s_when_a_classic_token_lacks_the_workflow_scope(
    async_client, db_session, httpx_mock: HTTPXMock
):
    """The shape that reached production: pushable, and refused at the last write.

    An Actions export always commits under `.github/workflows/`, which GitHub
    gates behind a scope of its own and refuses with a bare 404 — after the
    LLM call has been spent.
    """
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(url=_REPO_API, json=_push(True), headers={"X-OAuth-Scopes": "repo"})

    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "access_token": "ghp_no_workflow"},
    )

    assert resp.status_code == 422
    assert "workflow" in resp.json()["detail"]
    assert db_session.exec(select(CicdConfig)).all() == []


@pytest.mark.asyncio
async def test_put_accepts_a_classic_token_carrying_the_workflow_scope(
    async_client, db_session, httpx_mock: HTTPXMock
):
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(
        url=_REPO_API, json=_push(True), headers={"X-OAuth-Scopes": "repo, workflow"}
    )

    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "access_token": "ghp_full"},
    )

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_jenkins_needs_no_workflow_scope(async_client, db_session, httpx_mock: HTTPXMock):
    """A Jenkins export writes a Jenkinsfile, which GitHub gates behind nothing extra."""
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(url=_REPO_API, json=_push(True), headers={"X-OAuth-Scopes": "repo"})

    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "jenkins", "access_token": "ghp_no_workflow"},
    )

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_a_fine_grained_token_is_not_refused_for_an_unreported_scope(
    async_client, db_session, httpx_mock: HTTPXMock
):
    """Fine-grained PATs report no scopes — unknown must not read as missing."""
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(url=_REPO_API, json=_push(True))

    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "access_token": "github_pat_x"},
    )

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_put_502s_when_github_is_unreachable_and_persists_nothing(
    async_client, db_session, httpx_mock: HTTPXMock
):
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(url=_REPO_API, status_code=500)

    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "access_token": "ghp_x"},
    )

    assert resp.status_code == 502
    assert db_session.exec(select(CicdConfig)).all() == []


@pytest.mark.asyncio
async def test_put_422s_on_an_invalid_token_and_persists_nothing(
    async_client, db_session, httpx_mock: HTTPXMock
):
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(url=_REPO_API, status_code=401)

    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "access_token": "ghp_dead"},
    )

    assert resp.status_code == 422
    assert db_session.exec(select(CicdConfig)).all() == []


@pytest.mark.asyncio
async def test_put_rejects_an_unknown_provider(async_client, db_session):
    sprint_id = _sprint_id(db_session)

    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "circleci", "access_token": "ghp_x"},
    )

    assert resp.status_code == 422
    assert "circleci" in resp.json()["detail"]


# ── PUT: token resolution (typed → stored → repo's) ───────────────────


@pytest.mark.asyncio
async def test_a_blank_token_on_edit_falls_back_to_the_stored_one(
    async_client, db_session, httpx_mock: HTTPXMock
):
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(url=_REPO_API, json=_push(True))
    await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "access_token": "ghp_first"},
    )

    httpx_mock.add_response(url=_REPO_API, json=_push(True))
    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "access_token": "", "ci_environment_hint": "docker"},
    )

    assert resp.status_code == 200
    assert resp.json()["ci_environment_hint"] == "docker"
    stored = db_session.exec(select(CicdConfig)).one()
    assert decrypt_token(stored.access_token) == "ghp_first"


@pytest.mark.asyncio
async def test_a_blank_token_with_no_config_falls_back_to_the_repos_token(
    async_client, db_session, httpx_mock: HTTPXMock
):
    sprint_id = _sprint_id(db_session)
    repo = db_session.exec(select(Repo)).one()
    repo.github_token = encrypt_token("ghp_repo")
    db_session.add(repo)
    db_session.commit()

    httpx_mock.add_response(url=_REPO_API, json=_push(True))
    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config", json={"provider": "jenkins"}
    )

    assert resp.status_code == 200
    stored = db_session.exec(select(CicdConfig)).one()
    assert decrypt_token(stored.access_token) == "ghp_repo"


@pytest.mark.asyncio
async def test_a_blank_token_with_nothing_to_fall_back_on_422s(async_client, db_session):
    sprint_id = _sprint_id(db_session)

    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config", json={"provider": "github_actions"}
    )

    assert resp.status_code == 422
    assert "access token" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_a_provider_switch_keeps_the_stored_token(
    async_client, db_session, httpx_mock: HTTPXMock
):
    """Jenkins ships as a GitHub PR too, so the credential is GitHub's either way."""
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(url=_REPO_API, json=_push(True))
    await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "access_token": "ghp_kept"},
    )

    httpx_mock.add_response(url=_REPO_API, json=_push(True))
    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config", json={"provider": "jenkins"}
    )

    assert resp.status_code == 200
    assert resp.json()["provider"] == "jenkins"
    stored = db_session.exec(select(CicdConfig)).one()
    assert decrypt_token(stored.access_token) == "ghp_kept"


@pytest.mark.asyncio
async def test_an_edit_clears_a_hint_that_was_blanked(
    async_client, db_session, httpx_mock: HTTPXMock
):
    """Fields are assigned wholesale, so nothing survives a provider switch."""
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(url=_REPO_API, json=_push(True))
    await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={
            "provider": "github_actions",
            "access_token": "ghp_x",
            "ci_environment_hint": "runs on a self-hosted box",
        },
    )

    httpx_mock.add_response(url=_REPO_API, json=_push(True))
    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "ci_environment_hint": "   "},
    )

    assert resp.status_code == 200
    assert resp.json()["ci_environment_hint"] is None


@pytest.mark.asyncio
async def test_only_one_config_row_exists_after_repeated_saves(
    async_client, db_session, httpx_mock: HTTPXMock
):
    sprint_id = _sprint_id(db_session)
    for _ in range(3):
        httpx_mock.add_response(url=_REPO_API, json=_push(True))
        resp = await async_client.put(
            f"/api/sprints/{sprint_id}/cicd-config",
            json={"provider": "github_actions", "access_token": "ghp_x"},
        )
        assert resp.status_code == 200

    assert len(db_session.exec(select(CicdConfig)).all()) == 1


# ── A finished sprint may still connect and export ────────────────────


@pytest.mark.asyncio
async def test_put_succeeds_on_a_finished_sprint(async_client, db_session, httpx_mock: HTTPXMock):
    sprint_id = _sprint_id(db_session, active=False)
    httpx_mock.add_response(url=_REPO_API, json=_push(True))
    resp = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "access_token": "ghp_x"},
    )

    assert resp.status_code == 200


# ── DELETE ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_the_row_and_a_second_delete_404s(
    async_client, db_session, httpx_mock: HTTPXMock
):
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(url=_REPO_API, json=_push(True))
    await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "access_token": "ghp_x"},
    )

    first = await async_client.delete(f"/api/sprints/{sprint_id}/cicd-config")
    second = await async_client.delete(f"/api/sprints/{sprint_id}/cicd-config")

    assert first.status_code == 204
    assert second.status_code == 404
    assert db_session.exec(select(CicdConfig)).all() == []


@pytest.mark.asyncio
async def test_the_token_never_appears_in_any_response_body(
    async_client, db_session, httpx_mock: HTTPXMock
):
    sprint_id = _sprint_id(db_session)
    httpx_mock.add_response(url=_REPO_API, json=_push(True))

    saved = await async_client.put(
        f"/api/sprints/{sprint_id}/cicd-config",
        json={"provider": "github_actions", "access_token": "ghp_supersecret"},
    )
    fetched = await async_client.get(f"/api/sprints/{sprint_id}/cicd-config")

    assert "ghp_supersecret" not in saved.text
    assert "ghp_supersecret" not in fetched.text
    assert fetched.json()["provider"] == CicdProvider.GITHUB_ACTIONS.value
