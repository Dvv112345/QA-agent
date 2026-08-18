"""The CI/CD export task, end to end with GitHub mocked and the LLM stubbed."""

import json

import pytest
from pytest_httpx import HTTPXMock
from sqlmodel import select

from backend.models.database import (
    CicdConfig,
    CicdExport,
    CicdExportItem,
    CicdExportStatus,
    CicdProvider,
    Requirement,
    RequirementStatus,
    TestCase,
    TestEnvironmentAccess,
    TestEnvironmentStatus,
    TestPlan,
    TestPlanStatus,
)
from backend.services.llm import CicdFileItem, CicdIntegrationResult, HostEdit
from backend.tasks.export_cicd import export_cicd_task
from backend.tests.test_requirement_routes import _seed_sprint
from backend.utils.crypto import encrypt_token

_API = "https://api.github.com/repos/owner/repo"
_ENV_VARS = json.dumps({"BASE_URL": "https://app.test", "QA_PASSWORD": "hunter2secret"})

_WORKFLOW = """name: QA
on:
  workflow_dispatch:
jobs:
  qa:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""

_JENKINSFILE = """pipeline {
    agent any
    stages {
        stage('Build') {
            steps {
                sh 'make build'
            }
        }
    }
}
"""


def _result(**overrides) -> CicdIntegrationResult:
    fields = {
        "files": [CicdFileItem(path=".github/workflows/qa-agent.yml", content=_WORKFLOW)],
        "host_edit": None,
        "pr_title": "Add the QA suite",
        "pr_body": "It runs nightly.",
        "notes": None,
    }
    fields.update(overrides)
    return CicdIntegrationResult(**fields)


@pytest.fixture
def llm_stub(monkeypatch):
    """Records the generation call and returns a canned integration."""

    class _Stub:
        def __init__(self):
            self.calls: list[dict] = []
            self.result = _result()
            self.error: Exception | None = None

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            if self.error is not None:
                raise self.error
            return self.result

    stub = _Stub()
    import backend.services.llm as llm_module

    monkeypatch.setattr(llm_module, "generate_cicd_integration", stub)
    return stub


def _seed(db_session, *, provider=CicdProvider.GITHUB_ACTIONS, file_tree="src/app.py", cases=1):
    sprint = _seed_sprint(db_session)
    sprint.repo.file_tree = file_tree
    sprint.repo.github_token = encrypt_token("ghp_repo")
    db_session.add(sprint.repo)
    db_session.add(
        CicdConfig(sprint_id=sprint.id, provider=provider, access_token=encrypt_token("ghp_write"))
    )
    db_session.add(
        TestEnvironmentAccess(
            sprint_id=sprint.id,
            content="staging",
            original_content="staging",
            status=TestEnvironmentStatus.CONFIRMED,
            env_vars_json=_ENV_VARS,
        )
    )
    db_session.commit()

    requirement = Requirement(
        sprint_id=sprint.id,
        name="User login",
        description="d",
        original_description="d",
        status=RequirementStatus.CONFIRMED,
    )
    db_session.add(requirement)
    db_session.commit()
    plan = TestPlan(requirement_id=requirement.id, status=TestPlanStatus.APPROVED)
    db_session.add(plan)
    db_session.commit()

    seeded = []
    for index in range(cases):
        case = TestCase(
            test_plan_id=plan.id,
            position=index,
            title=f"Case {index}",
            steps="s",
            expected_result="e",
            case_type="functional",
            priority="high",
            script=f"print({index})\n",
            script_requirement_revision=requirement.content_revision,
            script_plan_revision=plan.content_revision,
            script_env_revision=sprint.test_environment.content_revision,
        )
        db_session.add(case)
        seeded.append(case)
    db_session.commit()
    db_session.refresh(sprint)
    return sprint, seeded


def _export(db_session, sprint, cases, **overrides) -> CicdExport:
    export = CicdExport(
        sprint_id=sprint.id,
        provider=sprint.cicd_config.provider,
        selected_case_ids_json=json.dumps([case.id for case in cases]),
        **overrides,
    )
    db_session.add(export)
    db_session.commit()
    db_session.refresh(export)
    return export


def _github_ok(httpx_mock: HTTPXMock, *, tree=None, ci_files=None):
    """The read + write sequence a successful export performs."""
    httpx_mock.add_response(url=_API, json={"full_name": "owner/repo", "default_branch": "main"})
    httpx_mock.add_response(
        url=f"{_API}/git/trees/main?recursive=1",
        json={"tree": tree or [{"path": "src/app.py", "type": "blob"}]},
    )
    for path, content in (ci_files or {}).items():
        import base64

        httpx_mock.add_response(
            url=f"{_API}/contents/{path}",
            json={
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode(),
                "size": len(content),
            },
        )
    httpx_mock.add_response(url=f"{_API}/git/ref/heads/main", json={"object": {"sha": "base-sha"}})
    httpx_mock.add_response(url=f"{_API}/git/trees", method="POST", json={"sha": "tree-sha"})
    httpx_mock.add_response(url=f"{_API}/git/commits", method="POST", json={"sha": "commit-sha"})
    httpx_mock.add_response(url=f"{_API}/git/refs", method="POST", json={"ref": "refs/heads/x"})
    httpx_mock.add_response(
        url=f"{_API}/pulls",
        method="POST",
        json={"number": 7, "html_url": "https://github.com/owner/repo/pull/7"},
    )


def _reload(db_session, export_id) -> CicdExport:
    db_session.expire_all()
    return db_session.get(CicdExport, export_id)


# ── Happy path ────────────────────────────────────────────────────────


def test_happy_path_writes_a_pr_and_lands_completed(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session)
    export = _export(db_session, sprint, cases)
    _github_ok(httpx_mock)

    export_cicd_task(export.id)

    row = _reload(db_session, export.id)
    assert row.status == CicdExportStatus.COMPLETED
    assert row.pr_url == "https://github.com/owner/repo/pull/7"
    assert row.pr_number == 7
    assert row.commit_sha == "commit-sha"
    assert row.branch_name.startswith(f"qa-agent/sprint-{sprint.id}-")
    assert row.last_heartbeat is None
    assert row.retry_count == 0
    assert row.ci_file_paths == [".github/workflows/qa-agent.yml"]
    assert row.variable_names == ["BASE_URL"]
    assert row.secret_names == ["QA_PASSWORD"]


def test_the_write_sequence_runs_in_order(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session)
    export = _export(db_session, sprint, cases)
    _github_ok(httpx_mock)

    export_cicd_task(export.id)

    writes = [str(request.url) for request in httpx_mock.get_requests() if request.method == "POST"]
    assert writes == [
        f"{_API}/git/trees",
        f"{_API}/git/commits",
        f"{_API}/git/refs",
        f"{_API}/pulls",
    ]


def test_the_scripts_are_committed_verbatim_under_their_layout_path(
    db_session, llm_stub, httpx_mock
):
    sprint, cases = _seed(db_session, cases=2)
    export = _export(db_session, sprint, cases)
    _github_ok(httpx_mock)

    export_cicd_task(export.id)

    tree = next(
        json.loads(request.content)
        for request in httpx_mock.get_requests()
        if str(request.url).endswith("/git/trees") and request.method == "POST"
    )
    by_path = {entry["path"]: entry["content"] for entry in tree["tree"]}
    assert by_path[f"qa-agent-tests/user-login_1/case-0_{cases[0].id}.py"] == "print(0)\n"
    assert by_path[f"qa-agent-tests/user-login_1/case-1_{cases[1].id}.py"] == "print(1)\n"


def test_items_are_written_only_after_a_successful_commit(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session, cases=2)
    export = _export(db_session, sprint, cases)
    _github_ok(httpx_mock)

    export_cicd_task(export.id)

    items = db_session.exec(select(CicdExportItem)).all()
    assert len(items) == 2
    assert {item.requirement_name for item in items} == {"User login"}
    assert _reload(db_session, export.id).case_count == 2


def test_the_pr_body_names_the_secrets_and_no_value(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session)
    export = _export(db_session, sprint, cases)
    _github_ok(httpx_mock)

    export_cicd_task(export.id)

    pull = next(
        json.loads(request.content)
        for request in httpx_mock.get_requests()
        if str(request.url).endswith("/pulls")
    )
    assert "QA_PASSWORD" in pull["body"]
    assert "BASE_URL" in pull["body"]
    assert "hunter2secret" not in pull["body"]


def test_no_env_value_or_token_appears_in_any_github_request(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session)
    export = _export(db_session, sprint, cases)
    _github_ok(httpx_mock)

    export_cicd_task(export.id)

    for request in httpx_mock.get_requests():
        body = request.content.decode() if request.content else ""
        assert "hunter2secret" not in body
        assert "https://app.test" not in body


# ── Failure paths ─────────────────────────────────────────────────────


def test_a_missing_config_fails_the_row_without_an_llm_call(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session)
    export = _export(db_session, sprint, cases)
    db_session.delete(sprint.cicd_config)
    db_session.commit()

    export_cicd_task(export.id)

    row = _reload(db_session, export.id)
    assert row.status == CicdExportStatus.FAILED
    assert "No CI/CD target" in row.error
    assert llm_stub.calls == []


def test_a_validation_failure_spends_a_retry_and_writes_nothing(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session)
    export = _export(db_session, sprint, cases)
    llm_stub.result = _result(
        files=[CicdFileItem(path="../../etc/passwd", content=_WORKFLOW)],
        host_edit=HostEdit(path="nowhere.yml", job_name="qa", job_body="steps: []"),
    )
    _github_ok(httpx_mock)

    export_cicd_task(export.id)

    row = _reload(db_session, export.id)
    assert row.status == CicdExportStatus.PENDING  # under the retry cap
    assert row.retry_count == 1
    writes = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert writes == []
    assert db_session.exec(select(CicdExportItem)).all() == []


def test_a_partial_write_failure_leaves_no_receipts(db_session, llm_stub, httpx_mock):
    """Tree and commit succeed, `create_ref` 422s — the middle case."""
    sprint, cases = _seed(db_session)
    export = _export(db_session, sprint, cases)
    httpx_mock.add_response(url=_API, json={"full_name": "owner/repo", "default_branch": "main"})
    httpx_mock.add_response(
        url=f"{_API}/git/trees/main?recursive=1",
        json={"tree": [{"path": "src/app.py", "type": "blob"}]},
    )
    httpx_mock.add_response(url=f"{_API}/git/ref/heads/main", json={"object": {"sha": "base-sha"}})
    httpx_mock.add_response(url=f"{_API}/git/trees", method="POST", json={"sha": "tree-sha"})
    httpx_mock.add_response(url=f"{_API}/git/commits", method="POST", json={"sha": "commit-sha"})
    httpx_mock.add_response(
        url=f"{_API}/git/refs",
        method="POST",
        status_code=422,
        json={"message": "Reference already exists"},
    )

    export_cicd_task(export.id)

    row = _reload(db_session, export.id)
    assert row.status == CicdExportStatus.PENDING
    assert row.retry_count == 1
    # The row must not claim anything that is not in the repository.
    assert row.pr_url is None
    assert row.branch_name is None
    assert row.commit_sha is None
    assert db_session.exec(select(CicdExportItem)).all() == []


def test_retry_exhaustion_lands_the_row_failed_with_no_items(
    db_session, llm_stub, httpx_mock, monkeypatch
):
    sprint, cases = _seed(db_session)
    export = _export(db_session, sprint, cases, retry_count=2)
    llm_stub.error = RuntimeError("provider exploded")
    _github_ok(httpx_mock)

    export_cicd_task(export.id)

    row = _reload(db_session, export.id)
    assert row.status == CicdExportStatus.FAILED
    assert "provider exploded" in row.error
    assert db_session.exec(select(CicdExportItem)).all() == []


def test_two_exports_started_together_get_distinct_branches(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session)
    first = _export(db_session, sprint, cases)
    second = _export(db_session, sprint, cases)
    _github_ok(httpx_mock)
    _github_ok(httpx_mock)

    export_cicd_task(first.id)
    export_cicd_task(second.id)

    branches = {
        _reload(db_session, first.id).branch_name,
        _reload(db_session, second.id).branch_name,
    }
    assert len(branches) == 2


def test_a_case_archived_between_selection_and_start_is_skipped(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session, cases=2)
    export = _export(db_session, sprint, cases)
    cases[0].archived = True
    db_session.add(cases[0])
    db_session.commit()
    _github_ok(httpx_mock)

    export_cicd_task(export.id)

    row = _reload(db_session, export.id)
    assert row.status == CicdExportStatus.COMPLETED
    assert row.case_count == 1


def test_every_case_ineligible_fails_the_row(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session)
    export = _export(db_session, sprint, cases)
    cases[0].script = None
    db_session.add(cases[0])
    db_session.commit()
    _github_ok(httpx_mock)

    export_cicd_task(export.id)

    row = _reload(db_session, export.id)
    assert row.status == CicdExportStatus.FAILED
    assert "eligible" in row.error


def test_a_stale_job_for_a_completed_export_is_a_no_op(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session)
    export = _export(db_session, sprint, cases, status=CicdExportStatus.COMPLETED)

    export_cicd_task(export.id)

    assert llm_stub.calls == []


def test_a_finished_sprint_still_exports(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session)
    sprint.active = False
    db_session.add(sprint)
    db_session.commit()
    export = _export(db_session, sprint, cases)
    _github_ok(httpx_mock)

    export_cicd_task(export.id)

    assert _reload(db_session, export.id).status == CicdExportStatus.COMPLETED


# ── Host edits ────────────────────────────────────────────────────────


def test_an_actions_host_edit_splices_a_job_into_the_existing_workflow(
    db_session, llm_stub, httpx_mock
):
    sprint, cases = _seed(db_session, file_tree="src/app.py\n.github/workflows/ci.yml")
    export = _export(db_session, sprint, cases)
    llm_stub.result = _result(
        files=[],
        host_edit=HostEdit(
            path=".github/workflows/ci.yml",
            job_name="qa-agent-e2e",
            job_body="runs-on: ubuntu-latest\nsteps:\n  - run: python a.py\n",
        ),
    )
    _github_ok(
        httpx_mock,
        tree=[
            {"path": "src/app.py", "type": "blob"},
            {"path": ".github/workflows/ci.yml", "type": "blob"},
        ],
        ci_files={".github/workflows/ci.yml": _WORKFLOW},
    )

    export_cicd_task(export.id)

    tree = next(
        json.loads(request.content)
        for request in httpx_mock.get_requests()
        if str(request.url).endswith("/git/trees") and request.method == "POST"
    )
    edited = {entry["path"]: entry["content"] for entry in tree["tree"]}
    assert "qa-agent-e2e:" in edited[".github/workflows/ci.yml"]
    # The team's own job survives.
    assert "echo hi" in edited[".github/workflows/ci.yml"]


def test_a_jenkins_export_splices_a_stage_and_leaves_the_rest_identical(
    db_session, llm_stub, httpx_mock
):
    sprint, cases = _seed(
        db_session, provider=CicdProvider.JENKINS, file_tree="src/app.py\nJenkinsfile"
    )
    export = _export(db_session, sprint, cases)
    stage = "stage('QA Agent E2E') {\n  steps {\n    sh 'python a.py'\n  }\n}"
    llm_stub.result = _result(
        files=[], host_edit=HostEdit(path="Jenkinsfile", job_name="QA", job_body=stage)
    )
    _github_ok(
        httpx_mock,
        tree=[{"path": "src/app.py", "type": "blob"}, {"path": "Jenkinsfile", "type": "blob"}],
        ci_files={"Jenkinsfile": _JENKINSFILE},
    )

    export_cicd_task(export.id)

    tree = next(
        json.loads(request.content)
        for request in httpx_mock.get_requests()
        if str(request.url).endswith("/git/trees") and request.method == "POST"
    )
    edited = {entry["path"]: entry["content"] for entry in tree["tree"]}["Jenkinsfile"]
    assert "stage('QA Agent E2E')" in edited
    assert "stage('Build')" in edited
    assert "make build" in edited


def test_an_unspliceable_jenkinsfile_falls_back_to_a_new_file(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(
        db_session, provider=CicdProvider.JENKINS, file_tree="src/app.py\nJenkinsfile"
    )
    export = _export(db_session, sprint, cases)
    stage = "stage('QA Agent E2E') {\n  steps {\n    sh 'python a.py'\n  }\n}"
    llm_stub.result = _result(
        files=[], host_edit=HostEdit(path="Jenkinsfile", job_name="QA", job_body=stage)
    )
    # A pipeline with no `stages` block — `insert_stage` answers None.
    _github_ok(
        httpx_mock,
        tree=[{"path": "src/app.py", "type": "blob"}, {"path": "Jenkinsfile", "type": "blob"}],
        ci_files={"Jenkinsfile": "pipeline {\n  agent any\n}\n"},
    )

    export_cicd_task(export.id)

    tree = next(
        json.loads(request.content)
        for request in httpx_mock.get_requests()
        if str(request.url).endswith("/git/trees") and request.method == "POST"
    )
    paths = {entry["path"] for entry in tree["tree"]}
    assert "Jenkinsfile.qa-agent" in paths
    assert "Jenkinsfile" not in paths

    pull = next(
        json.loads(request.content)
        for request in httpx_mock.get_requests()
        if str(request.url).endswith("/pulls")
    )
    assert "Jenkinsfile.qa-agent" in pull["body"]


# ── The prompt ────────────────────────────────────────────────────────


def test_the_generation_call_receives_names_never_values(db_session, llm_stub, httpx_mock):
    sprint, cases = _seed(db_session)
    export = _export(db_session, sprint, cases)
    _github_ok(httpx_mock)

    export_cicd_task(export.id)

    call = llm_stub.calls[0]
    assert call["variable_names"] == ["BASE_URL"]
    assert call["secret_names"] == ["QA_PASSWORD"]
    assert "hunter2secret" not in json.dumps({k: str(v) for k, v in call.items()})


def test_no_file_tree_means_no_read_file_tool(db_session, llm_stub, httpx_mock):
    """An empty repository has no CI to match — a plain completion is right."""
    sprint, cases = _seed(db_session, file_tree=None)
    export = _export(db_session, sprint, cases)
    httpx_mock.add_response(url=_API, json={"full_name": "owner/repo", "default_branch": "main"})
    httpx_mock.add_response(url=f"{_API}/git/trees/main?recursive=1", status_code=404)
    httpx_mock.add_response(url=f"{_API}/git/ref/heads/main", json={"object": {"sha": "base-sha"}})
    httpx_mock.add_response(url=f"{_API}/git/trees", method="POST", json={"sha": "tree-sha"})
    httpx_mock.add_response(url=f"{_API}/git/commits", method="POST", json={"sha": "commit-sha"})
    httpx_mock.add_response(url=f"{_API}/git/refs", method="POST", json={"ref": "refs/heads/x"})
    httpx_mock.add_response(
        url=f"{_API}/pulls", method="POST", json={"number": 1, "html_url": "https://x/pull/1"}
    )

    export_cicd_task(export.id)

    assert llm_stub.calls[0]["read_file"] is None
    pull = next(
        json.loads(request.content)
        for request in httpx_mock.get_requests()
        if str(request.url).endswith("/pulls")
    )
    assert "could not read this repository" in pull["body"]
