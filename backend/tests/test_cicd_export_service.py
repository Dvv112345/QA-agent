"""The deterministic half of a CI/CD export, and the gate over the model's half."""

import pytest

from backend.services.cicd_export import (
    CicdValidationError,
    jenkins_stage_block,
    pr_body,
    qa_job_steps,
    sanitize_actions_name,
    sanitize_jenkins_id,
    script_files,
    script_path,
    slugify,
    validate,
)
from backend.services.llm import CicdFileItem, CicdIntegrationResult, HostEdit


class _Requirement:
    def __init__(self, id_, name):
        self.id = id_
        self.name = name


class _Plan:
    def __init__(self, requirement):
        self.requirement = requirement


class _Case:
    """The three fields the layout functions read off a TestCase."""

    def __init__(self, id_, title, requirement, script="print(1)\n"):
        self.id = id_
        self.title = title
        self.test_plan = _Plan(requirement)
        self.script = script


def _result(**overrides) -> CicdIntegrationResult:
    fields = {
        "files": [],
        "host_edit": None,
        "pr_title": "Add QA suite",
        "pr_body": "prose",
        "notes": None,
    }
    fields.update(overrides)
    return CicdIntegrationResult(**fields)


_WORKFLOW = """name: QA
on:
  workflow_dispatch:
jobs:
  qa:
    runs-on: ubuntu-latest
    steps:
      - run: python qa-agent-tests/a_1/b_2.py
"""


# ── Structural pins on the result schema ──────────────────────────────


def test_the_result_cannot_carry_a_test_script():
    """The CI/CD analogue of keeping test planning code-blind."""
    fields = set(CicdIntegrationResult.model_fields)

    assert not any("script" in name for name in fields)
    assert fields == {"files", "host_edit", "pr_title", "pr_body", "notes"}


def test_a_file_item_has_no_action_field():
    """`files` is create-only, so a "modify" branch is not expressible."""
    assert set(CicdFileItem.model_fields) == {"path", "content"}


def test_a_host_edit_carries_a_fragment_not_a_file():
    assert set(HostEdit.model_fields) == {"path", "job_name", "job_body"}


# ── Layout ────────────────────────────────────────────────────────────


def test_slugify_collapses_and_truncates():
    assert slugify("User Login (v2)!", "case") == "user-login-v2"
    assert len(slugify("x" * 200, "case")) == 40


def test_slugify_falls_back_when_the_result_would_be_empty():
    """Reachable: a title written entirely in non-ASCII characters."""
    assert slugify("ログイン機能", "case") == "case"
    assert slugify("---", "requirement") == "requirement"


def test_the_case_id_is_appended_unconditionally():
    requirement = _Requirement(3, "User login")
    first = _Case(7, "Happy path", requirement)
    second = _Case(8, "Happy path", requirement)

    assert script_path(first) == "qa-agent-tests/user-login_3/happy-path_7.py"
    assert script_path(second) == "qa-agent-tests/user-login_3/happy-path_8.py"


def test_the_requirement_id_is_appended_unconditionally():
    """Two requirements that slugify alike must not share a directory."""
    first = _Case(1, "Case", _Requirement(3, "User login"))
    second = _Case(2, "Case", _Requirement(4, "user-login"))

    assert script_path(first).startswith("qa-agent-tests/user-login_3/")
    assert script_path(second).startswith("qa-agent-tests/user-login_4/")


def test_a_title_that_slugifies_to_nothing_still_produces_a_usable_path():
    case = _Case(9, "テスト", _Requirement(2, "ログイン"))

    assert script_path(case) == "qa-agent-tests/requirement_2/case_9.py"


def test_script_files_carries_the_cached_script_verbatim():
    case = _Case(7, "Happy path", _Requirement(3, "Login"), script="print('exact')\n")

    assert script_files([case]) == {"qa-agent-tests/login_3/happy-path_7.py": "print('exact')\n"}


# ── Name sanitizing ───────────────────────────────────────────────────


def test_the_two_sanitizers_differ_where_their_namespaces_differ():
    """A GITHUB_-prefixed name is legal for Jenkins and reserved for Actions."""
    assert sanitize_actions_name("GITHUB_TOKEN") == "QA_GITHUB_TOKEN"
    assert sanitize_jenkins_id("GITHUB_TOKEN") == "GITHUB_TOKEN"

    assert sanitize_actions_name("api.base-url") == "API_BASE_URL"
    assert sanitize_jenkins_id("api.base-url") == "api.base-url"


def test_a_sanitized_name_never_starts_with_a_digit():
    assert sanitize_actions_name("2FA_SECRET").startswith("QA_")
    assert sanitize_jenkins_id("2fa").startswith("qa_")


# ── The deterministic block ───────────────────────────────────────────


def test_qa_job_steps_installs_browsers_and_runs_one_python_per_script():
    steps = qa_job_steps(["qa-agent-tests/a_1/b_2.py", "qa-agent-tests/a_1/c_3.py"], [], [])

    runs = [step.get("run", "") for step in steps]
    assert any("playwright install" in run and "chromium" in run for run in runs)
    assert "python qa-agent-tests/a_1/b_2.py" in runs
    assert "python qa-agent-tests/a_1/c_3.py" in runs


def test_qa_job_steps_maps_a_sanitized_secret_name_back_through_env():
    steps = qa_job_steps(["a.py"], ["BASE_URL"], ["GITHUB_PAT"])

    env = steps[-1]["env"]
    assert env["BASE_URL"] == "${{ vars.BASE_URL }}"
    # The original name is the env key; the sanitized one is the reference.
    assert env["GITHUB_PAT"] == "${{ secrets.QA_GITHUB_PAT }}"


def test_qa_job_steps_reuses_the_repos_own_install_and_tops_up_the_rest():
    steps = qa_job_steps(["a.py"], [], [], repo_install=["pip install -r requirements.txt"])

    runs = [step.get("run", "") for step in steps]
    assert "pip install -r requirements.txt" in runs
    assert any(run.startswith("pip install ") and "playwright" in run for run in runs)


def test_jenkins_stage_block_emits_the_same_sequence_as_sh_steps():
    block = jenkins_stage_block(["qa-agent-tests/a_1/b_2.py"], ["BASE_URL"], ["QA_PASSWORD"])

    assert "stage('QA Agent E2E')" in block
    assert "playwright install" in block
    assert "sh 'python qa-agent-tests/a_1/b_2.py'" in block
    assert "withCredentials(" in block
    assert "credentialsId: 'QA_PASSWORD'" in block
    assert 'BASE_URL = "${env.BASE_URL}"' in block


def test_jenkins_stage_block_brace_balances_so_it_can_be_spliced():
    from backend.services.jenkins_text import braces_balance

    block = jenkins_stage_block(["a.py"], ["BASE_URL"], ["PWD_SECRET"])

    assert braces_balance(block) is True


# ── The gate: path allowlist ──────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "../etc/passwd",
        "/etc/passwd",
        "src/app.py",
        "qa-agent-tests/../../secrets.env",
        ".github/workflows/nested/deep.yml",
    ],
)
def test_validate_drops_a_path_outside_the_allowlist(path):
    result = _result(files=[CicdFileItem(path=path, content=_WORKFLOW)])

    dropped = validate(result, [], {}, provider_is_actions=True)

    assert dropped == [path]


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/qa.yml",
        ".github/workflows/qa.yaml",
        ".github/actions/qa/action.yml",
        "ci/run-qa.sh",
        "qa-agent-tests/README.md",
    ],
)
def test_validate_accepts_each_allowed_shape(path):
    content = _WORKFLOW if path.endswith((".yml", ".yaml")) else "echo hi"
    result = _result(files=[CicdFileItem(path=path, content=content)])

    assert validate(result, [], {}, provider_is_actions=True) == []


def test_validate_accepts_a_jenkinsfile_path():
    result = _result(
        files=[
            CicdFileItem(
                path="Jenkinsfile.qa-agent",
                content="pipeline {\n  agent any\n  stages {\n    stage('QA') { }\n  }\n}\n",
            )
        ]
    )

    assert validate(result, [], {}, provider_is_actions=False) == []


# ── The gate: structural floor ────────────────────────────────────────


def test_validate_rejects_yaml_that_will_not_parse():
    result = _result(files=[CicdFileItem(path=".github/workflows/qa.yml", content="a: [1\nb: :")])

    with pytest.raises(CicdValidationError, match="not valid YAML"):
        validate(result, [], {}, provider_is_actions=True)


def test_validate_rejects_a_workflow_with_no_jobs():
    content = "name: QA\non:\n  workflow_dispatch:\n"
    result = _result(files=[CicdFileItem(path=".github/workflows/qa.yml", content=content)])

    with pytest.raises(CicdValidationError, match="no jobs"):
        validate(result, [], {}, provider_is_actions=True)


def test_validate_rejects_a_workflow_with_no_triggers():
    content = "name: QA\njobs:\n  qa:\n    steps: []\n"
    result = _result(files=[CicdFileItem(path=".github/workflows/qa.yml", content=content)])

    with pytest.raises(CicdValidationError, match="no triggers"):
        validate(result, [], {}, provider_is_actions=True)


def test_validate_accepts_a_bare_on_key():
    """YAML 1.1 parses it as the boolean True — the gate must accept both."""
    content = "name: QA\non:\n  workflow_dispatch:\njobs:\n  qa:\n    steps: []\n"
    result = _result(files=[CicdFileItem(path=".github/workflows/qa.yml", content=content)])

    assert validate(result, [], {}, provider_is_actions=True) == []


def test_validate_rejects_a_jenkinsfile_failing_the_floor_check():
    """Previously this passed vacuously — the R5 pin."""
    result = _result(files=[CicdFileItem(path="Jenkinsfile", content="def x = 1\n")])

    with pytest.raises(CicdValidationError, match="not a usable Jenkinsfile"):
        validate(result, [], {}, provider_is_actions=False)


# ── The gate: reference resolution ────────────────────────────────────


def test_validate_rejects_an_actions_reference_we_never_supplied():
    content = _WORKFLOW.replace(
        "runs-on: ubuntu-latest", "runs-on: ubuntu-latest\n    env:\n      X: ${{ secrets.NOPE }}"
    )
    result = _result(files=[CicdFileItem(path=".github/workflows/qa.yml", content=content)])

    with pytest.raises(CicdValidationError, match="NOPE"):
        validate(result, ["BASE_URL"], {}, provider_is_actions=True)


def test_validate_accepts_a_supplied_actions_reference():
    content = _WORKFLOW.replace(
        "runs-on: ubuntu-latest",
        "runs-on: ubuntu-latest\n    env:\n      X: ${{ vars.BASE_URL }}",
    )
    result = _result(files=[CicdFileItem(path=".github/workflows/qa.yml", content=content)])

    assert validate(result, ["BASE_URL"], {}, provider_is_actions=True) == []


def test_validate_rejects_unsupplied_jenkins_references():
    """The Actions pattern never matches Groovy, so this gate must be its own."""
    content = (
        "pipeline {\n  agent any\n  stages {\n    stage('QA') {\n"
        '      steps { sh "curl ${env.NOT_SUPPLIED}" }\n    }\n  }\n}\n'
    )
    result = _result(files=[CicdFileItem(path="Jenkinsfile", content=content)])

    with pytest.raises(CicdValidationError, match="NOT_SUPPLIED"):
        validate(result, ["BASE_URL"], {}, provider_is_actions=False)


def test_validate_rejects_an_unsupplied_jenkins_credentials_id():
    content = (
        "pipeline {\n  agent any\n  stages {\n    stage('QA') {\n"
        "      steps {\n"
        "        withCredentials([string(credentialsId: 'NOT_SUPPLIED', variable: 'P')]) {\n"
        "          sh 'python a.py'\n"
        "        }\n      }\n    }\n  }\n}\n"
    )
    result = _result(files=[CicdFileItem(path="Jenkinsfile", content=content)])

    with pytest.raises(CicdValidationError, match="NOT_SUPPLIED"):
        validate(result, ["BASE_URL"], {}, provider_is_actions=False)


def test_validate_accepts_supplied_jenkins_references():
    content = (
        "pipeline {\n  agent any\n  stages {\n    stage('QA') {\n"
        "      steps {\n"
        "        withCredentials([string(credentialsId: 'QA_PASSWORD', variable: 'P')]) {\n"
        '          sh "curl ${env.BASE_URL}"\n'
        "        }\n      }\n    }\n  }\n}\n"
    )
    result = _result(files=[CicdFileItem(path="Jenkinsfile", content=content)])

    assert validate(result, ["BASE_URL", "QA_PASSWORD"], {}, provider_is_actions=False) == []


# ── The gate: host edits ──────────────────────────────────────────────

_JOB_BODY = "runs-on: ubuntu-latest\nsteps:\n  - run: python qa-agent-tests/a_1/b_2.py\n"


def test_validate_rejects_a_host_edit_for_a_file_this_export_never_fetched():
    edit = HostEdit(path="ci/e2e.sh", job_name="qa", job_body=_JOB_BODY)
    result = _result(host_edit=edit)

    with pytest.raises(CicdValidationError, match="did not fetch"):
        validate(result, [], {".github/workflows/ci.yml": _WORKFLOW}, provider_is_actions=True)


def test_validate_accepts_a_host_edit_against_a_fetched_file():
    edit = HostEdit(path=".github/workflows/ci.yml", job_name="qa", job_body=_JOB_BODY)
    result = _result(host_edit=edit)

    outcome = validate(
        result, [], {".github/workflows/ci.yml": _WORKFLOW}, provider_is_actions=True
    )

    assert outcome == []


def test_validate_rejects_a_job_body_that_is_not_a_mapping_with_steps():
    edit = HostEdit(path=".github/workflows/ci.yml", job_name="qa", job_body="just a string")
    result = _result(host_edit=edit)

    with pytest.raises(CicdValidationError, match="mapping carrying 'steps'"):
        validate(result, [], {".github/workflows/ci.yml": _WORKFLOW}, provider_is_actions=True)


# ── The PR trailer ────────────────────────────────────────────────────


def test_pr_body_lists_every_name_and_no_value():
    body = pr_body(
        "The model's prose.",
        "Sprint 1",
        3,
        ["BASE_URL"],
        ["QA_PASSWORD"],
        [],
        provider_is_actions=True,
    )

    assert "The model's prose." in body
    assert "Sprint 1" in body
    assert "BASE_URL" in body
    assert "QA_PASSWORD" in body
    assert "hunter2secret" not in body
    assert "staging.example.com" not in body


def test_pr_body_names_a_sanitized_reference_when_it_differs():
    body = pr_body("p", "S", 1, [], ["GITHUB_PAT"], [], provider_is_actions=True)

    assert "QA_GITHUB_PAT" in body


def test_pr_body_names_every_dropped_path():
    body = pr_body("p", "S", 1, [], [], ["../etc/passwd"], provider_is_actions=True)

    assert "../etc/passwd" in body
    assert "not written" in body


def test_pr_body_says_so_when_the_sprint_defines_no_environment():
    body = pr_body("p", "S", 1, [], [], [], provider_is_actions=True)

    assert "No environment variables" in body


def test_pr_body_carries_extra_notices_and_generation_notes():
    body = pr_body(
        "p",
        "S",
        1,
        [],
        [],
        [],
        provider_is_actions=True,
        notes="Adapted the runner.",
        extra_notices=["The repository was not read during generation."],
    )

    assert "Adapted the runner." in body
    assert "The repository was not read during generation." in body
