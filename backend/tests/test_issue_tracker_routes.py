"""Tests for backend/routes/issue_tracker.py — connect, edit, disconnect.

``services.issue_tracker.verify`` is monkeypatched throughout, the same
treatment ``services.llm`` functions get elsewhere: this module's job is
persistence and classification, and the transport already has its own
suite against ``pytest-httpx``.

No test holds a real credential — every token here is a dummy.
"""

import pytest
from sqlmodel import select

from backend.models.database import (
    ExploratoryRun,
    ExploratoryRunStatus,
    IssueTrackerConfig,
    RequirementStatus,
)
from backend.services import issue_tracker
from backend.services.issue_tracker import TrackerError, TrackerUnavailableError
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.utils.crypto import decrypt_token, encrypt_token

_JIRA_PAYLOAD = {
    "provider": "jira",
    "target": "QA",
    "base_url": "https://acme.atlassian.net",
    "account_email": "qa@acme.test",
    "api_token": "dummy-jira-token",
    "issue_type": "Bug",
}

_GITHUB_PAYLOAD = {
    "provider": "github",
    "target": "acme/shop",
    "api_token": "dummy-github-token",
}


class _VerifyStub:
    """Records the config it was handed and answers however the test wants."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list = []

    def __call__(self, config):
        self.calls.append(config)
        if self.error is not None:
            raise self.error
        return f"{config.provider} · {config.target}"


@pytest.fixture
def verify_stub(monkeypatch):
    stub = _VerifyStub()
    monkeypatch.setattr(issue_tracker, "verify", stub)
    return stub


def _config_row(db_session, sprint_id: int) -> IssueTrackerConfig:
    db_session.expire_all()
    return db_session.exec(
        select(IssueTrackerConfig).where(IssueTrackerConfig.sprint_id == sprint_id)
    ).one()


# ── GET ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_null_when_nothing_is_connected(async_client, db_session):
    sprint = _seed_sprint(db_session)
    resp = await async_client.get(f"/api/sprints/{sprint.id}/issue-tracker")
    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.asyncio
async def test_get_unknown_sprint_is_404(async_client):
    resp = await async_client.get("/api/sprints/999/issue-tracker")
    assert resp.status_code == 404


# ── PUT: create ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_persists_an_encrypted_token_and_omits_it_from_the_response(
    async_client, db_session, verify_stub
):
    sprint = _seed_sprint(db_session)

    resp = await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)

    assert resp.status_code == 200
    body = resp.json()
    assert "api_token" not in body
    assert body["provider"] == "jira"
    assert body["target"] == "QA"
    assert body["target_label"] == "Jira · QA"
    row = _config_row(db_session, sprint.id)
    assert row.api_token != "dummy-jira-token"
    assert decrypt_token(row.api_token) == "dummy-jira-token"


@pytest.mark.asyncio
async def test_put_verifies_with_the_plaintext_token(async_client, db_session, verify_stub):
    """Verifying with the ciphertext would report a perfectly good token
    as invalid, so the ordering is the contract."""
    sprint = _seed_sprint(db_session)

    await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)

    assert verify_stub.calls[0].api_token == "dummy-jira-token"


@pytest.mark.asyncio
async def test_github_config_stores_no_jira_fields(async_client, db_session, verify_stub):
    sprint = _seed_sprint(db_session)

    resp = await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_GITHUB_PAYLOAD)

    assert resp.status_code == 200
    assert resp.json()["target_label"] == "GitHub · acme/shop"
    row = _config_row(db_session, sprint.id)
    assert (row.base_url, row.account_email, row.issue_type) == (None, None, None)


@pytest.mark.asyncio
async def test_unknown_sprint_is_404(async_client, verify_stub):
    resp = await async_client.put("/api/sprints/999/issue-tracker", json=_JIRA_PAYLOAD)
    assert resp.status_code == 404


# ── PUT: validation and verification failures ─────────────────────────


@pytest.mark.parametrize(
    "missing",
    ["base_url", "account_email", "issue_type", "target"],
)
@pytest.mark.asyncio
async def test_jira_requires_its_own_fields(async_client, db_session, verify_stub, missing):
    """Validated as a combination rather than in the schema, so the error
    names the field instead of reading as a malformed request."""
    sprint = _seed_sprint(db_session)
    payload = {**_JIRA_PAYLOAD, missing: ""}

    resp = await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=payload)

    assert resp.status_code == 422
    assert verify_stub.calls == []


@pytest.mark.asyncio
async def test_jira_site_must_be_absolute(async_client, db_session, verify_stub):
    sprint = _seed_sprint(db_session)
    payload = {**_JIRA_PAYLOAD, "base_url": "acme.atlassian.net"}

    resp = await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=payload)

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_github_target_must_be_owner_slash_repo(async_client, db_session, verify_stub):
    sprint = _seed_sprint(db_session)
    payload = {**_GITHUB_PAYLOAD, "target": "shop"}

    resp = await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=payload)

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unknown_provider_is_422(async_client, db_session, verify_stub):
    sprint = _seed_sprint(db_session)
    payload = {**_GITHUB_PAYLOAD, "provider": "gitlab"}

    resp = await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=payload)

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_refusal_is_422_and_persists_nothing(async_client, db_session, monkeypatch):
    monkeypatch.setattr(issue_tracker, "verify", _VerifyStub(TrackerError("bad token")))
    sprint = _seed_sprint(db_session)

    resp = await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)

    assert resp.status_code == 422
    assert resp.json()["detail"] == "bad token"
    db_session.expire_all()
    assert db_session.get(sprint.__class__, sprint.id).issue_tracker is None


@pytest.mark.asyncio
async def test_unreachable_tracker_is_502_and_persists_nothing(
    async_client, db_session, monkeypatch
):
    """502 rather than 422: nobody has anything to fix, and retrying may
    simply work."""
    monkeypatch.setattr(issue_tracker, "verify", _VerifyStub(TrackerUnavailableError("down")))
    sprint = _seed_sprint(db_session)

    resp = await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)

    assert resp.status_code == 502
    db_session.expire_all()
    assert db_session.get(sprint.__class__, sprint.id).issue_tracker is None


@pytest.mark.asyncio
async def test_missing_encryption_key_is_500_with_the_generate_message(
    async_client, db_session, monkeypatch, verify_stub
):
    """conftest sets a key, so this clears it explicitly. The message is
    the one actionable thing in the response — keep it intact."""
    import backend.utils.crypto as crypto

    monkeypatch.setattr(crypto, "_cipher", None)
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    sprint = _seed_sprint(db_session)

    resp = await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)

    assert resp.status_code == 500
    assert "Fernet.generate_key" in resp.json()["detail"]


# ── PUT: edit ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_put_updates_the_row_in_place(async_client, db_session, verify_stub):
    """Updating rather than delete-and-insert, so the unique FK is never
    transiently violated."""
    sprint = _seed_sprint(db_session)
    await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)
    first_id = (await async_client.get(f"/api/sprints/{sprint.id}/issue-tracker")).json()["id"]

    resp = await async_client.put(
        f"/api/sprints/{sprint.id}/issue-tracker", json={**_JIRA_PAYLOAD, "target": "PLATFORM"}
    )

    assert resp.status_code == 200
    assert resp.json()["id"] == first_id
    assert resp.json()["target"] == "PLATFORM"
    db_session.expire_all()
    rows = db_session.exec(select(IssueTrackerConfig)).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_blank_token_on_a_same_provider_edit_reuses_the_stored_one(
    async_client, db_session, verify_stub
):
    """Re-entering a secret to change a project key is the kind of
    friction that gets tokens pasted into chat windows."""
    sprint = _seed_sprint(db_session)
    await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)

    resp = await async_client.put(
        f"/api/sprints/{sprint.id}/issue-tracker",
        json={**_JIRA_PAYLOAD, "target": "PLATFORM", "api_token": ""},
    )

    assert resp.status_code == 200
    # The decrypted original reached verify — not the ciphertext, and not
    # an empty string.
    assert verify_stub.calls[-1].api_token == "dummy-jira-token"
    assert decrypt_token(_config_row(db_session, sprint.id).api_token) == "dummy-jira-token"


@pytest.mark.asyncio
async def test_omitted_token_is_treated_as_blank(async_client, db_session, verify_stub):
    sprint = _seed_sprint(db_session)
    await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)
    payload = {k: v for k, v in _JIRA_PAYLOAD.items() if k != "api_token"}

    resp = await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=payload)

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_blank_token_on_first_connect_is_422(async_client, db_session, verify_stub):
    sprint = _seed_sprint(db_session)

    resp = await async_client.put(
        f"/api/sprints/{sprint.id}/issue-tracker", json={**_JIRA_PAYLOAD, "api_token": ""}
    )

    assert resp.status_code == 422
    assert verify_stub.calls == []


@pytest.mark.asyncio
async def test_switching_provider_with_a_blank_token_is_422(async_client, db_session, verify_stub):
    """A Jira API token is meaningless to GitHub — reusing it silently
    would verify nothing and store a credential that can never work."""
    sprint = _seed_sprint(db_session)
    await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)

    resp = await async_client.put(
        f"/api/sprints/{sprint.id}/issue-tracker",
        json={**_GITHUB_PAYLOAD, "api_token": ""},
    )

    assert resp.status_code == 422
    assert "changing provider" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_switching_jira_to_github_nulls_the_jira_fields(
    async_client, db_session, verify_stub
):
    """A stale Jira site lingering on a GitHub config would be invisible
    in the UI and wrong in every outbound request."""
    sprint = _seed_sprint(db_session)
    await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)

    resp = await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_GITHUB_PAYLOAD)

    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "github"
    assert body["base_url"] is None
    assert body["account_email"] is None
    assert body["issue_type"] is None
    row = _config_row(db_session, sprint.id)
    assert (row.base_url, row.account_email, row.issue_type) == (None, None, None)


@pytest.mark.asyncio
async def test_editing_leaves_already_filed_findings_untouched(
    async_client, db_session, verify_stub
):
    """Their URLs still point where they were actually filed, and their
    tracker_target keeps them out of the new tracker's dedup window."""
    from backend.tests.test_sprints import (
        _seed_test_case,
        _seed_test_case_execution,
        _seed_test_execution,
        _seed_test_plan,
        _seed_test_run,
    )

    sprint = _seed_sprint(db_session)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    plan = _seed_test_plan(db_session, requirement)
    run = _seed_test_run(db_session, sprint)
    execution = _seed_test_execution(db_session, run, requirement)
    case = _seed_test_case_execution(
        db_session, execution, _seed_test_case(db_session, plan), status="failed"
    )
    case.tracker_issue_key = "QA-142"
    case.tracker_issue_url = "https://acme.atlassian.net/browse/QA-142"
    case.tracker_target = "jira:QA"
    db_session.commit()

    await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)
    await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_GITHUB_PAYLOAD)

    db_session.expire_all()
    reloaded = db_session.get(case.__class__, case.id)
    assert reloaded.tracker_issue_key == "QA-142"
    assert reloaded.tracker_target == "jira:QA"
    assert reloaded.tracker_issue_url == "https://acme.atlassian.net/browse/QA-142"


@pytest.mark.asyncio
async def test_unreadable_stored_token_asks_for_a_new_one(async_client, db_session, verify_stub):
    """A corrupted ciphertext is the user's problem to fix once, not a
    500 they can do nothing about."""
    sprint = _seed_sprint(db_session)
    db_session.add(
        IssueTrackerConfig(
            sprint_id=sprint.id, provider="jira", target="QA", api_token="not-fernet"
        )
    )
    db_session.commit()

    resp = await async_client.put(
        f"/api/sprints/{sprint.id}/issue-tracker", json={**_JIRA_PAYLOAD, "api_token": ""}
    )

    assert resp.status_code == 422
    assert "Enter it again" in resp.json()["detail"]


# ── DELETE ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_the_config(async_client, db_session, verify_stub):
    sprint = _seed_sprint(db_session)
    await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)

    resp = await async_client.delete(f"/api/sprints/{sprint.id}/issue-tracker")

    assert resp.status_code == 204
    assert (await async_client.get(f"/api/sprints/{sprint.id}/issue-tracker")).json() is None


@pytest.mark.asyncio
async def test_delete_without_a_config_is_404(async_client, db_session):
    sprint = _seed_sprint(db_session)
    resp = await async_client.delete(f"/api/sprints/{sprint.id}/issue-tracker")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_succeeds_while_a_run_is_in_flight(async_client, db_session, verify_stub):
    """Blocking would put a settings change behind whatever a worker
    happens to be doing, to prevent an outcome already handled: that
    run's export fails into tracker_error and its findings wait on the
    run page for a Retry."""
    sprint = _seed_sprint(db_session)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    db_session.add(
        ExploratoryRun(
            sprint_id=sprint.id,
            requirement_id=requirement.id,
            base_url_env_vars_csv="APP_URL",
            status=ExploratoryRunStatus.RUNNING,
        )
    )
    db_session.commit()
    await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)

    resp = await async_client.delete(f"/api/sprints/{sprint.id}/issue-tracker")

    assert resp.status_code == 204


# ── Auth ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_routes_are_authenticated(monkeypatch, db_session):
    from backend.tests.test_auth_routes import _make_client

    async with _make_client(monkeypatch, db_session) as client:
        assert (await client.get("/api/sprints/1/issue-tracker")).status_code == 401
        assert (await client.put("/api/sprints/1/issue-tracker", json={})).status_code == 401
        assert (await client.delete("/api/sprints/1/issue-tracker")).status_code == 401


# ── Token hygiene ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_never_appears_in_any_response_body(async_client, db_session, verify_stub):
    sprint = _seed_sprint(db_session)

    put = await async_client.put(f"/api/sprints/{sprint.id}/issue-tracker", json=_JIRA_PAYLOAD)
    get = await async_client.get(f"/api/sprints/{sprint.id}/issue-tracker")

    assert "dummy-jira-token" not in put.text
    assert "dummy-jira-token" not in get.text
    # Nor the ciphertext, which is only ever an implementation detail.
    assert encrypt_token("x")[:10] not in get.text
