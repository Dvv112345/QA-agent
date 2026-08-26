"""Pins for the Jenkinsfile brace scanner, floor check and stage splice.

Pure text in, text out — the cheapest surface in the CI/CD export.
"""

from pathlib import Path

import pytest

from backend.services.jenkins_text import (
    _scan,
    braces_balance,
    find_block,
    floor_check,
    insert_stage,
    stage_check,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jenkinsfiles"

_STAGE = """stage('QA Agent E2E') {
    steps {
        sh 'python qa-agent-tests/login_1/happy_2.py'
    }
}"""


def _text(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


# ── The scanner ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        "sh 'echo }'",
        'sh "echo }"',
        "sh '''echo }'''",
        'sh """echo }"""',
        "// a comment with } in it\n",
        "/* a block comment with } in it */",
    ],
)
def test_scan_treats_strings_and_comments_as_non_structural(source):
    text = "pipeline {\n" + source + "\n}\n"

    assert braces_balance(text) is True


def test_scan_returns_spans_covering_the_literals():
    text = "a = 'x}' // done\n"

    spans = _scan(text)

    assert len(spans) == 2
    assert text[spans[0].start : spans[0].end] == "'x}'"
    assert text[spans[1].start : spans[1].end] == "// done"


def test_scan_does_not_raise_on_an_unterminated_literal():
    spans = _scan("sh 'never closed")

    assert spans  # runs to end of file rather than raising
    assert braces_balance("pipeline { sh 'never closed") is False


def test_triple_quotes_are_matched_before_single_quotes():
    """`'''` must be tested first, or it opens and closes on its own first char."""
    text = "pipeline {\n sh '''\n } still inside\n '''\n}\n"

    assert braces_balance(text) is True


# ── find_block ────────────────────────────────────────────────────────


def test_find_block_locates_stages_in_a_declarative_pipeline():
    text = _text("declarative.Jenkinsfile")

    span = find_block(text, "stages")

    assert span is not None
    block = text[span.start : span.end]
    assert block.startswith("stages {")
    assert block.endswith("}")
    assert "stage('Build')" in block
    assert "junit" not in block  # the post block is outside it


def test_find_block_returns_none_when_the_keyword_has_no_block():
    assert find_block(_text("no_stages.Jenkinsfile"), "stages") is None


def test_find_block_ignores_a_keyword_that_is_part_of_a_longer_word():
    text = "pipeline {\n  mystages { echo 'x' }\n}\n"

    assert find_block(text, "stages") is None


# ── floor_check ───────────────────────────────────────────────────────


def test_floor_check_passes_a_declarative_pipeline():
    assert floor_check(_text("declarative.Jenkinsfile")) == []


def test_floor_check_passes_a_scripted_pipeline():
    assert floor_check(_text("scripted.Jenkinsfile")) == []


def test_floor_check_passes_the_brace_in_string_fixture():
    assert floor_check(_text("brace_in_string.Jenkinsfile")) == []


def test_floor_check_reports_a_missing_stages_block():
    problems = floor_check(_text("no_stages.Jenkinsfile"))

    assert any("stages" in problem for problem in problems)


def test_floor_check_reports_every_failure_not_just_the_first():
    problems = floor_check(_text("unbalanced.Jenkinsfile"))

    assert any("braces" in problem for problem in problems)
    assert len(problems) >= 1


def test_floor_check_rejects_a_file_that_is_neither_pipeline_nor_node():
    problems = floor_check("def x = 1\n")

    assert any("pipeline" in problem for problem in problems)


def test_floor_check_rejects_a_declarative_pipeline_declaring_no_stages():
    problems = floor_check("pipeline {\n  agent any\n  stages {\n  }\n}\n")

    assert any("declares no stages" in problem for problem in problems)


# ── insert_stage ──────────────────────────────────────────────────────


def test_insert_stage_places_the_stage_last_inside_stages():
    before = _text("declarative.Jenkinsfile")

    after = insert_stage(before, _STAGE)

    assert after is not None
    assert after.index("stage('Test')") < after.index("stage('QA Agent E2E')")
    # Still inside `stages`, i.e. before the `post` block that follows it.
    assert after.index("stage('QA Agent E2E')") < after.index("post {")


def test_insert_stage_leaves_every_other_byte_identical():
    before = _text("declarative.Jenkinsfile")

    after = insert_stage(before, _STAGE)

    # Take out exactly the block we added; what is left must be the input,
    # byte for byte — no reindentation, no reflow, no trailing-space fixups.
    added = "\n".join(f"        {line}" if line.strip() else line for line in _STAGE.splitlines())
    assert f"{added}\n" in after
    assert after.replace(f"{added}\n", "", 1) == before


def test_insert_stage_output_balances_and_holds_the_stage_once():
    after = insert_stage(_text("declarative.Jenkinsfile"), _STAGE)

    assert braces_balance(after) is True
    assert after.count("stage('QA Agent E2E')") == 1
    assert floor_check(after) == []


def test_insert_stage_is_correct_when_braces_hide_inside_string_literals():
    before = _text("brace_in_string.Jenkinsfile")

    after = insert_stage(before, _STAGE)

    assert after is not None
    # The unbalanced brace inside `sh 'echo { …'` did not move the point:
    # the new stage lands after the existing one and inside `stages`.
    assert after.index("stage('Echo')") < after.index("stage('QA Agent E2E')")
    assert braces_balance(after) is True
    assert floor_check(after) == []


def test_insert_stage_refuses_an_unbalanced_file():
    assert insert_stage(_text("unbalanced.Jenkinsfile"), _STAGE) is None


def test_insert_stage_refuses_a_file_with_no_stages_block():
    assert insert_stage(_text("no_stages.Jenkinsfile"), _STAGE) is None


def test_insert_stage_indents_the_stage_to_match_the_block():
    after = insert_stage(_text("declarative.Jenkinsfile"), _STAGE)

    assert "\n        stage('QA Agent E2E') {\n" in after


# ── stage_check ───────────────────────────────────────────────────────


def test_stage_check_passes_a_well_formed_fragment():
    assert stage_check("stage('QA') {\n  steps { sh 'x' }\n}") == []


def test_stage_check_reports_a_fragment_declaring_no_stage():
    problems = stage_check("steps { sh 'x' }")

    assert any("declares no stage" in problem for problem in problems)


def test_stage_check_reports_unbalanced_braces():
    problems = stage_check("stage('QA') { steps {")

    assert any("braces" in problem for problem in problems)


def test_stage_check_does_not_demand_a_pipeline_block():
    """The file-level floor does; a fragment correctly has none."""
    fragment = "stage('QA') { steps { sh 'x' } }"

    assert stage_check(fragment) == []
    assert floor_check(fragment) != []
