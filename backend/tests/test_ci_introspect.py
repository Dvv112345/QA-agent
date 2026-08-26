"""Fixture-driven pins for the CI introspection layer.

Pure text in, facts out — no LLM, no network, no database.
"""

from pathlib import Path

import pytest

from backend.services.ci_introspect import (
    WorkflowEditError,
    add_job,
    classify_purpose,
    host_hazards,
    is_reusable_workflow,
    parse_composite_action,
    parse_workflow,
    render_facts,
    triggers_allow_job,
)

_WORKFLOWS = Path(__file__).resolve().parent / "fixtures" / "workflows"


def _facts(filename: str):
    text = (_WORKFLOWS / filename).read_text(encoding="utf-8")
    return parse_workflow(f".github/workflows/{filename}", text)


def _text(filename: str) -> str:
    return (_WORKFLOWS / filename).read_text(encoding="utf-8")


# ── Extraction ────────────────────────────────────────────────────────


def test_e2e_workflow_parses_to_the_expected_facts():
    facts = _facts("e2e_playwright.yml")

    assert facts.name == "E2E Tests"
    assert set(facts.triggers) == {"workflow_dispatch", "schedule"}
    assert facts.runs_on == "ubuntu-22.04"
    assert facts.python_version == "3.12"
    assert facts.node_version is None
    assert facts.installs_browsers is True
    assert facts.has_services is True
    assert facts.has_concurrency is True
    assert "CI" in facts.env_keys
    assert any("pip install" in command for command in facts.install_commands)
    assert facts.purpose == "e2e"


def test_backend_ci_parses_to_the_expected_facts():
    facts = _facts("backend_ci.yml")

    assert facts.triggers == ("pull_request",)
    assert facts.runs_on == "ubuntu-latest"
    assert facts.python_version == "3.12"
    assert facts.installs_browsers is False
    assert facts.has_services is False
    assert facts.purpose == "test"


def test_deploy_workflow_keeps_its_list_runner_and_env_names():
    facts = _facts("deploy.yml")

    assert facts.runs_on == "self-hosted, deploy"
    assert facts.purpose == "deploy"


def test_frontend_style_workflow_reports_node_and_local_actions():
    facts = _facts("defaults_working_dir.yml")

    assert facts.node_version == "22"
    assert facts.local_actions == ("./.github/actions/report",)


def test_bare_on_key_yields_its_triggers_rather_than_nothing():
    """YAML 1.1 parses an unquoted `on:` as the boolean True — the footgun pin."""
    facts = _facts("bare_on_key.yml")

    assert set(facts.triggers) == {"workflow_dispatch", "schedule"}


def test_malformed_workflow_returns_none_and_does_not_raise():
    assert _facts("malformed.yml") is None


def test_runs_on_is_unresolvable_for_a_matrix_expression():
    assert _facts("bare_on_key.yml").runs_on is None


def test_runs_on_is_unresolvable_for_a_group_labels_object():
    assert _facts("defaults_working_dir.yml").runs_on is None


# ── Classification ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("e2e_playwright.yml", "e2e"),
        ("backend_ci.yml", "test"),
        ("deploy.yml", "deploy"),
        ("codeql.yml", "noise"),
    ],
)
def test_classify_purpose_sorts_the_four_fixtures(filename, expected):
    assert _facts(filename).purpose == expected


def test_noise_wins_over_every_other_reading():
    """A CodeQL workflow that happens to be scheduled is still noise."""
    facts = _facts("codeql.yml")

    assert classify_purpose(facts) == "noise"


def test_browser_install_makes_a_deploy_named_workflow_read_as_e2e():
    """Precedence is stated: e2e (evidence) beats deploy (naming)."""
    text = _text("deploy.yml").replace(
        "run: ./scripts/deploy.sh", "run: playwright install chromium"
    )
    facts = parse_workflow(".github/workflows/deploy.yml", text)

    assert facts.installs_browsers is True
    assert facts.purpose == "e2e"


# ── Trigger gating ────────────────────────────────────────────────────


def test_triggers_allow_job_refuses_a_pull_request_host():
    assert triggers_allow_job(_facts("backend_ci.yml")) is False


def test_triggers_allow_job_accepts_dispatch_plus_schedule():
    assert triggers_allow_job(_facts("e2e_playwright.yml")) is True


def test_triggers_allow_job_refuses_a_workflow_with_no_triggers_at_all():
    facts = parse_workflow(".github/workflows/x.yml", "name: X\njobs:\n  a:\n    steps: []\n")

    assert facts.triggers == ()
    assert triggers_allow_job(facts) is False


# ── Composite actions and reusable workflows ──────────────────────────


def test_parse_composite_action_reports_a_required_input_with_no_default():
    action = parse_composite_action(_text("composite_action.yml"))

    assert action.is_composite is True
    assert action.required_inputs_without_default == ("report-path",)


def test_is_reusable_workflow_detects_workflow_call():
    from backend.services.ci_introspect import safe_yaml

    doc = safe_yaml().load(_text("reusable.yml"))

    assert is_reusable_workflow(doc) is True


def test_is_reusable_workflow_is_false_for_an_ordinary_workflow():
    from backend.services.ci_introspect import safe_yaml

    doc = safe_yaml().load(_text("e2e_playwright.yml"))

    assert is_reusable_workflow(doc) is False


# ── Hazards ───────────────────────────────────────────────────────────


def test_host_hazards_reports_working_directory_env_and_concurrency():
    hazards = " ".join(host_hazards(_facts("defaults_working_dir.yml")))

    assert "working-directory" in hazards
    assert "NODE_ENV" in hazards
    assert "concurrency" in hazards


def test_host_hazards_is_empty_for_a_workflow_that_sets_none_of_them():
    assert host_hazards(_facts("backend_ci.yml")) == []


# ── Rendering ─────────────────────────────────────────────────────────


def test_render_facts_names_the_source_file_beside_every_value():
    rendered = render_facts([_facts("backend_ci.yml"), _facts("e2e_playwright.yml")])

    assert ".github/workflows/backend_ci.yml" in rendered
    assert ".github/workflows/e2e_playwright.yml" in rendered
    # Two workflows, two blocks — never merged into one repo-wide summary.
    assert rendered.count("- purpose:") == 2
    backend_block, e2e_block = rendered.split("\n\n")
    assert "ubuntu-latest" in backend_block
    assert "ubuntu-22.04" in e2e_block


def test_render_facts_says_so_when_there_is_no_ci_at_all():
    assert "No existing CI workflows" in render_facts([])


# ── Editing ───────────────────────────────────────────────────────────

_JOB_BODY = {"runs-on": "ubuntu-latest", "steps": [{"run": "python qa-agent-tests/a_1.py"}]}


def test_add_job_preserves_comments_quoting_and_key_order():
    before = _text("bare_on_key.yml")

    after = add_job(before, "qa-agent-e2e", _JOB_BODY)

    assert "# The trigger key is written bare" in after
    assert "qa-agent-e2e:" in after
    # Everything the file already said survives untouched.
    for line in ("name: Nightly smoke", "- run: npm ci", "os: [ubuntu-latest, windows-latest]"):
        assert line in after
    assert after.index("smoke:") < after.index("qa-agent-e2e:")


def test_add_job_refuses_a_document_with_no_jobs_key():
    with pytest.raises(WorkflowEditError, match="no 'jobs' mapping"):
        add_job("name: Not a workflow\non:\n  push:\n", "qa", _JOB_BODY)


def test_add_job_refuses_a_document_that_does_not_parse():
    with pytest.raises(WorkflowEditError, match="does not parse"):
        add_job(_text("malformed.yml"), "qa", _JOB_BODY)


def test_add_job_uniquifies_a_colliding_name_rather_than_overwriting():
    before = _text("backend_ci.yml")

    after = add_job(before, "test", _JOB_BODY)

    assert "test-qa-agent:" in after
    # The team's own job is still there, with its own steps.
    assert "python -m pytest -v" in after


def test_add_job_uniquifies_repeatedly_when_the_first_alternative_is_taken():
    once = add_job(_text("backend_ci.yml"), "test", _JOB_BODY)

    twice = add_job(once, "test", _JOB_BODY)

    assert "test-qa-agent:" in twice
    assert "test-qa-agent-2:" in twice
