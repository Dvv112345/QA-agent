"""LLM client for requirement clarity and test-environment analysis.

Talks to any OpenAI-compatible API (DeepSeek by default via
``OPENAI_BASE_URL``) using the sync OpenAI SDK — the callers are RQ worker
tasks or routes that offload to a thread (``asyncio.to_thread``).  JSON
output is requested with the portable ``json_object`` response format plus
explicit shape instructions in the prompt, then validated with a pydantic
model; anything that goes wrong surfaces as ``LLMError``.  Prompt text and
prompt-assembly helpers live in ``services/llm_prompts.py``.

The API key is never logged and prompts are never logged at INFO level.
"""

from __future__ import annotations

import json
import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar

import certifi
import httpx
import openai
from sqlmodel import SQLModel

from backend.config import (
    EXPLORATORY_MAX_CHARTERS,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    OPENAI_TIMEOUT,
    TEST_EXECUTION_TOOL_ROUNDS,
    TEST_PLAN_TOOL_ROUNDS,
)
from backend.models.database import SfdipotArea, TestCasePriority
from backend.services.llm_prompts import (
    BROWSER_TOOLS,
    CHARTER_SYSTEM_PROMPT,
    CHECK_SYSTEM_PROMPT,
    ENV_VARS_SYSTEM_PROMPT,
    EXPLORATION_SUMMARY_SYSTEM_PROMPT,
    EXPLORATION_SYSTEM_PROMPT,
    READ_FILE_TOOL,
    REVISE_SYSTEM_PROMPT,
    SESSION_WRAPUP_PROMPT,
    SPLIT_PRD_SYSTEM_PROMPT,
    TEST_ENV_CHECK_SYSTEM_PROMPT,
    TEST_ENV_REVISE_SYSTEM_PROMPT,
    TEST_PLAN_REVISE_SYSTEM_PROMPT,
    TEST_PLAN_SYSTEM_PROMPT,
    TEST_SCRIPT_DIAGNOSIS_SYSTEM_PROMPT,
    TEST_SCRIPT_SYSTEM_PROMPT,
    ExploratorySessionLike,
    TestCaseLike,
    charter_context,
    context_sections,
    env_vars_context,
    exploration_context,
    exploration_summary_context,
    requirements_section,
    test_plan_context,
    test_script_context,
)

# On Windows, SSL_CERT_FILE may point to a non-existent file which breaks
# httpx's default SSL context; use certifi like github_utils does.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Missing API key, API failure, or malformed model output."""


class ClarityResult(SQLModel):
    clear: bool
    clarifying_question: str | None = None
    rewritten_description: str | None = None


class TestEnvironmentResult(SQLModel):
    __test__ = False  # tell pytest this "Test*" name is not a test class

    sufficient: bool
    clarifying_question: str | None = None
    rewritten_content: str | None = None


_ResultT = TypeVar("_ResultT", bound=SQLModel)


_client: openai.OpenAI | None = None


def _get_client() -> openai.OpenAI:
    """Build the OpenAI client once per process; raise ``LLMError`` without a key."""
    global _client
    if _client is None:
        if not OPENAI_API_KEY:
            raise LLMError(
                "OPENAI_API_KEY is not configured — requirement analysis is unavailable."
            )
        _client = openai.OpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL,
            timeout=OPENAI_TIMEOUT,
            http_client=httpx.Client(verify=_SSL_CONTEXT, timeout=OPENAI_TIMEOUT),
        )
    return _client


def _complete(system_prompt: str, user_prompt: str, model_cls: type[_ResultT]) -> _ResultT:
    """Run one JSON-mode chat completion and validate the result."""
    client = _get_client()
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
    except openai.OpenAIError as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc

    try:
        return model_cls.model_validate(json.loads(content or ""))
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMError(f"LLM returned malformed output: {exc}") from exc


def check_clarity(
    name: str,
    description: str,
    readme: str | None,
    file_tree: str | None,
) -> ClarityResult:
    """Judge whether a requirement is clear enough to write tests against."""
    parts = context_sections(readme, file_tree)
    parts.append(f"Requirement name: {name}\nRequirement description:\n{description}")
    result = _complete(CHECK_SYSTEM_PROMPT, "\n\n".join(parts), ClarityResult)
    if not result.clear and not result.clarifying_question:
        raise LLMError("LLM judged the requirement unclear but gave no clarifying question.")
    return result


def revise_requirement(
    name: str,
    description: str,
    question: str,
    answer: str,
    readme: str | None,
    file_tree: str | None,
) -> ClarityResult:
    """Rewrite a requirement using the clarification Q&A and re-judge clarity."""
    parts = context_sections(readme, file_tree)
    parts.append(
        f"Requirement name: {name}\n"
        f"Current requirement description:\n{description}\n\n"
        f"Clarifying question that was asked:\n{question}\n\n"
        f"User's answer:\n{answer}"
    )
    result = _complete(REVISE_SYSTEM_PROMPT, "\n\n".join(parts), ClarityResult)
    if not result.clear and not result.clarifying_question:
        raise LLMError("LLM judged the requirement unclear but gave no clarifying question.")
    if not result.rewritten_description:
        raise LLMError("LLM revision did not include a rewritten description.")
    return result


# ── PRD splitting ─────────────────────────────────────────────────────


class PrdRequirementItem(SQLModel):
    name: str
    description: str


class PrdSplitResult(SQLModel):
    requirements: list[PrdRequirementItem]


def split_prd(prd_text: str, readme: str | None, file_tree: str | None) -> PrdSplitResult:
    """Split an uploaded PRD document into discrete requirements.

    Returns an *empty* result when the model finds no requirements — the
    caller decides how to report that to the user.  A partially empty item
    (name without description or vice versa) is malformed output.
    """
    parts = context_sections(readme, file_tree)
    parts.append(f"PRD document:\n---\n{prd_text}\n---")
    result = _complete(SPLIT_PRD_SYSTEM_PROMPT, "\n\n".join(parts), PrdSplitResult)

    cleaned: list[PrdRequirementItem] = []
    for item in result.requirements:
        name = item.name.strip()
        description = item.description.strip()
        if not name and not description:
            continue  # drop fully blank entries rather than failing the upload
        if not name or not description:
            raise LLMError("LLM returned a requirement with a missing name or description.")
        cleaned.append(PrdRequirementItem(name=name, description=description))
    return PrdSplitResult(requirements=cleaned)


# ── Test environment access ───────────────────────────────────────────


def check_test_environment(
    content: str,
    requirements: list[tuple[str, str]],
    readme: str | None,
    file_tree: str | None,
) -> TestEnvironmentResult:
    """Judge whether a test-environment access description is sufficient."""
    parts = [requirements_section(requirements)]
    parts.extend(context_sections(readme, file_tree))
    parts.append(f"Test environment access description:\n{content}")
    result = _complete(TEST_ENV_CHECK_SYSTEM_PROMPT, "\n\n".join(parts), TestEnvironmentResult)
    if not result.sufficient and not result.clarifying_question:
        raise LLMError("LLM judged the description insufficient but gave no clarifying question.")
    return result


def revise_test_environment(
    content: str,
    question: str,
    answer: str,
    requirements: list[tuple[str, str]],
    readme: str | None,
    file_tree: str | None,
) -> TestEnvironmentResult:
    """Rewrite the access description using the Q&A and re-judge sufficiency."""
    parts = [requirements_section(requirements)]
    parts.extend(context_sections(readme, file_tree))
    parts.append(
        f"Current test environment access description:\n{content}\n\n"
        f"Clarifying question that was asked:\n{question}\n\n"
        f"User's answer:\n{answer}"
    )
    result = _complete(TEST_ENV_REVISE_SYSTEM_PROMPT, "\n\n".join(parts), TestEnvironmentResult)
    if not result.sufficient and not result.clarifying_question:
        raise LLMError("LLM judged the description insufficient but gave no clarifying question.")
    if not result.rewritten_content:
        raise LLMError("LLM revision did not include rewritten content.")
    return result


# ── Test plans ────────────────────────────────────────────────────────


class TestCaseResult(SQLModel):
    __test__ = False  # tell pytest this "Test*" name is not a test class

    title: str
    preconditions: str | None = None
    steps: list[str]
    expected_result: str
    case_type: str
    priority: TestCasePriority


class TestPlanResult(SQLModel):
    __test__ = False  # tell pytest this "Test*" name is not a test class

    complexity: Literal["low", "medium", "high"]
    summary: str
    cases: list[TestCaseResult]


def _validate_test_plan(result: TestPlanResult) -> TestPlanResult:
    """Reject structurally valid but unusable plans (empty/blank fields)."""
    if not result.cases:
        raise LLMError("LLM returned a test plan with no test cases.")
    for case in result.cases:
        if not case.title.strip() or not case.expected_result.strip() or not case.case_type.strip():
            raise LLMError("LLM returned a test case with a blank title, expected result, or type.")
        if not any(step.strip() for step in case.steps):
            raise LLMError("LLM returned a test case with no executable steps.")
    return result


def _parse_json(content: str | None, model_cls: type[_ResultT]) -> _ResultT:
    try:
        return model_cls.model_validate(json.loads(content or ""))
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMError(f"LLM returned malformed output: {exc}") from exc


def _complete_with_tools(
    system_prompt: str,
    user_prompt: str,
    model_cls: type[_ResultT],
    read_file: Callable[[str], str] | None,
    on_round: Callable[[], None],
    tool_rounds: int,
) -> _ResultT:
    """Run a bounded read_file tool loop and parse the final JSON result.

    Every round sends ``tools`` together with strict JSON mode — verified
    against DeepSeek (2026-07-16): the combination works, and omitting
    ``response_format`` yields unparseable (fenced) final answers.  After
    ``tool_rounds`` tool rounds, one final call is forced with
    ``tool_choice="none"``.  ``read_file`` never raises — it returns error
    strings the model can react to.  With ``read_file=None`` (no file tree)
    this degrades to a plain completion.
    """
    if read_file is None:
        return _complete(system_prompt, user_prompt, model_cls)

    client = _get_client()
    budget_prompt = (
        f"{user_prompt}\n\n"
        f"You may use up to {tool_rounds} rounds of read_file calls "
        "before you must answer with the required JSON object."
    )
    messages: list = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": budget_prompt},
    ]

    for round_no in range(1, tool_rounds + 1):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=[READ_FILE_TOOL],
                response_format={"type": "json_object"},
            )
        except openai.BadRequestError as exc:
            if round_no == 1:
                # Provider rejects tools — regenerate context-only.
                logger.warning(
                    "LLM provider rejected tool calls; falling back to context-only generation: %s",
                    exc,
                )
                return _complete(system_prompt, user_prompt, model_cls)
            raise LLMError(f"LLM request failed: {exc}") from exc
        except openai.OpenAIError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        on_round()
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            return _parse_json(message.content, model_cls)

        messages.append(message)
        for tool_call in tool_calls:
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
                requested_path = str(arguments.get("path", ""))
            except (json.JSONDecodeError, AttributeError):
                requested_path = ""
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": read_file(requested_path),
                }
            )
        remaining = tool_rounds - round_no
        messages[-1]["content"] += (
            f"\n[read_file budget: {remaining} of {tool_rounds} rounds "
            "remaining — respond with the required JSON object when you have enough context]"
        )

    # Round cap hit — force the final answer.
    messages.append(
        {
            "role": "user",
            "content": (
                "Your read_file budget is exhausted. "
                "Respond now with only the required JSON object."
            ),
        }
    )
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            tools=[READ_FILE_TOOL],
            tool_choice="none",
            response_format={"type": "json_object"},
        )
    except openai.OpenAIError as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc
    on_round()
    return _parse_json(response.choices[0].message.content, model_cls)


def generate_test_plan(
    name: str,
    description: str,
    sibling_names: list[str],
    test_env_content: str | None,
    readme: str | None,
    file_tree: str | None,
    read_file: Callable[[str], str] | None,
    on_round: Callable[[], None],
) -> TestPlanResult:
    """Generate a structured test plan for one requirement.

    Runs a bounded ``read_file`` tool loop (``TEST_PLAN_TOOL_ROUNDS``);
    with ``read_file=None`` falls back to a plain single completion.
    """
    parts = test_plan_context(name, description, sibling_names, test_env_content, readme, file_tree)
    result = _complete_with_tools(
        TEST_PLAN_SYSTEM_PROMPT,
        "\n\n".join(parts),
        TestPlanResult,
        read_file,
        on_round,
        TEST_PLAN_TOOL_ROUNDS,
    )
    return _validate_test_plan(result)


def revise_test_plan(
    name: str,
    description: str,
    sibling_names: list[str],
    test_env_content: str | None,
    readme: str | None,
    file_tree: str | None,
    current_plan_json: str,
    feedback: str,
    read_file: Callable[[str], str] | None,
    on_round: Callable[[], None],
) -> TestPlanResult:
    """Revise a draft test plan per user feedback (same loop + validation)."""
    parts = test_plan_context(name, description, sibling_names, test_env_content, readme, file_tree)
    parts.append(f"Current test plan (JSON):\n{current_plan_json}\n\nUser's feedback:\n{feedback}")
    result = _complete_with_tools(
        TEST_PLAN_REVISE_SYSTEM_PROMPT,
        "\n\n".join(parts),
        TestPlanResult,
        read_file,
        on_round,
        TEST_PLAN_TOOL_ROUNDS,
    )
    return _validate_test_plan(result)


# ── Test execution ────────────────────────────────────────────────────


class EnvVarsResult(SQLModel):
    variables: dict[str, str]


class TestScriptResult(SQLModel):
    __test__ = False  # tell pytest this "Test*" name is not a test class

    script: str


class ScriptDiagnosisResult(SQLModel):
    __test__ = False  # tell pytest this "Test*" name is not a test class

    classification: Literal["script_bug", "app_bug"]
    fixed_script: str | None = None
    explanation: str


def _validate_env_vars(result: EnvVarsResult) -> EnvVarsResult:
    """Reject an empty extraction or blank keys/values (unusable either way)."""
    if not result.variables:
        raise LLMError("LLM returned no environment variables.")
    for key, value in result.variables.items():
        if not key.strip() or not value.strip():
            raise LLMError("LLM returned an environment variable with a blank name or value.")
    return result


def _validate_test_script(result: TestScriptResult) -> TestScriptResult:
    """Reject a blank script (unusable either way)."""
    if not result.script.strip():
        raise LLMError("LLM returned a blank test script.")
    return result


def _validate_diagnosis(result: ScriptDiagnosisResult) -> ScriptDiagnosisResult:
    """Reject a blank explanation, or a script_bug verdict with no fix."""
    if not result.explanation.strip():
        raise LLMError("LLM returned a diagnosis with no explanation.")
    if result.classification == "script_bug" and not (result.fixed_script or "").strip():
        raise LLMError("LLM classified a script_bug but returned no fixed script.")
    return result


def generate_env_vars(content: str, readme: str | None, file_tree: str | None) -> EnvVarsResult:
    """Extract structured access details from a sufficient access description.

    Plain single completion, like ``check_test_environment`` — no tool loop,
    since interpreting free text needs no repo access.
    """
    parts = env_vars_context(content, readme, file_tree)
    result = _complete(ENV_VARS_SYSTEM_PROMPT, "\n\n".join(parts), EnvVarsResult)
    return _validate_env_vars(result)


def generate_test_script(
    name: str,
    description: str,
    test_case: TestCaseLike,
    env_var_names: list[str],
    readme: str | None,
    file_tree: str | None,
    read_file: Callable[[str], str] | None,
    on_round: Callable[[], None],
) -> TestScriptResult:
    """Generate a Playwright script for one test case.

    Runs a bounded ``read_file`` tool loop (``TEST_EXECUTION_TOOL_ROUNDS``);
    with ``read_file=None`` falls back to a plain single completion.
    """
    parts = test_script_context(name, description, test_case, env_var_names, readme, file_tree)
    result = _complete_with_tools(
        TEST_SCRIPT_SYSTEM_PROMPT,
        "\n\n".join(parts),
        TestScriptResult,
        read_file,
        on_round,
        TEST_EXECUTION_TOOL_ROUNDS,
    )
    return _validate_test_script(result)


def diagnose_and_fix_script(
    name: str,
    description: str,
    test_case: TestCaseLike,
    env_var_names: list[str],
    readme: str | None,
    file_tree: str | None,
    script: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    read_file: Callable[[str], str] | None,
    on_round: Callable[[], None],
) -> ScriptDiagnosisResult:
    """Classify a failed run as a script bug or app bug; fix script bugs in the same call."""
    parts = test_script_context(name, description, test_case, env_var_names, readme, file_tree)
    parts.append(
        f"Script that was run:\n---\n{script}\n---\n\n"
        f"Exit code: {exit_code}\n"
        f"stdout:\n---\n{stdout}\n---\n\n"
        f"stderr:\n---\n{stderr}\n---"
    )
    result = _complete_with_tools(
        TEST_SCRIPT_DIAGNOSIS_SYSTEM_PROMPT,
        "\n\n".join(parts),
        ScriptDiagnosisResult,
        read_file,
        on_round,
        TEST_EXECUTION_TOOL_ROUNDS,
    )
    return _validate_diagnosis(result)


# ── Exploratory testing ───────────────────────────────────────────────


class CharterItem(SQLModel):
    charter: str
    sfdipot_areas: list[str] = []


class CharterResult(SQLModel):
    charters: list[CharterItem]
    base_url_env_vars: list[str] = []


class SessionWrapUpResult(SQLModel):
    notes: str
    stop_reason: str | None = None


class ExplorationSummaryResult(SQLModel):
    summary: str


@dataclass
class ExplorationLoopResult:
    """What one exploratory session produced.

    Findings are deliberately absent: they are persisted as they happen, via
    the ``record_finding`` executor, which is the only way to capture a
    screenshot while the problem is still on screen.
    """

    notes: str
    stop_reason: str
    actions_used: int
    action_log: list[str]


# Stop reasons recorded on ExploratorySession.stop_reason.
STOP_CHARTER_COMPLETE = "charter_complete"
STOP_ACTION_CAP = "action_cap"

# Placeholder replacing a pruned snapshot tool result. Snapshots are the only
# large item in the conversation and a ref from many actions ago is stale
# anyway, so older ones are dropped verbatim rather than summarized by a
# second LLM call (see the brainstorm's Risk 1).
_PRUNED_SNAPSHOT = "[snapshot replaced to save context — take a fresh one if you need refs]"

# Consecutive identical tool calls before the model gets a nudge instead of
# another execution.
_REPEAT_NUDGE_THRESHOLD = 3


def _validate_charters(result: CharterResult) -> CharterResult:
    """Reject charter sets that are empty, over-cap, blank, or mis-tagged."""
    if not result.charters:
        raise LLMError("LLM returned no charters.")
    if len(result.charters) > EXPLORATORY_MAX_CHARTERS:
        raise LLMError(
            f"LLM returned {len(result.charters)} charters, "
            f"above the cap of {EXPLORATORY_MAX_CHARTERS}."
        )
    valid_areas = {area.value for area in SfdipotArea}
    for item in result.charters:
        if not item.charter.strip():
            raise LLMError("LLM returned a blank charter.")
        for area in item.sfdipot_areas:
            if area not in valid_areas:
                raise LLMError(f"LLM returned an unknown SFDIPOT area: {area!r}.")
    if not result.base_url_env_vars:
        raise LLMError("LLM nominated no environment variable for the application URL.")
    return result


def generate_charters(
    name: str,
    description: str,
    covered_cases: list[TestCaseLike],
    env_var_names: list[str],
    readme: str | None,
    file_tree: str | None,
) -> CharterResult:
    """Draft SBTM charters for one requirement and nominate its app URLs.

    Plain single completion — no tool loop, the same cheap profile as
    ``generate_env_vars``.  Whether the nominated variable *names* actually
    exist in the environment map is checked by the caller: this module never
    sees ``env_vars`` values, only their names.
    """
    parts = charter_context(name, description, covered_cases, env_var_names, readme, file_tree)
    result = _complete(CHARTER_SYSTEM_PROMPT, "\n\n".join(parts), CharterResult)
    return _validate_charters(result)


def summarize_exploration(
    name: str,
    description: str,
    sessions: list[ExploratorySessionLike],
) -> ExplorationSummaryResult:
    """Synthesize a run's session sheets into a per-requirement summary.

    Plain single completion.  Callers treat failure as non-fatal — the
    findings, not this paragraph, are the run's deliverable.
    """
    parts = exploration_summary_context(name, description, sessions)
    result = _complete(
        EXPLORATION_SUMMARY_SYSTEM_PROMPT, "\n\n".join(parts), ExplorationSummaryResult
    )
    if not result.summary.strip():
        raise LLMError("LLM returned a blank exploration summary.")
    return result


def _prune_snapshots(messages: list, snapshot_indices: list[int], window: int) -> None:
    """Replace all but the newest *window* snapshot results with a placeholder.

    Mutates ``messages`` in place.  Only ``tool`` results are touched —
    assistant messages are never pruned, because the model's own commentary
    between calls is small and is the narrative thread that keeps a long
    session coherent (it also doubles as the raw material for the wrap-up).
    """
    for index in snapshot_indices[:-window] if window > 0 else snapshot_indices:
        if messages[index]["content"] != _PRUNED_SNAPSHOT:
            messages[index]["content"] = _PRUNED_SNAPSHOT


def run_exploration_loop(
    name: str,
    description: str,
    charter: str,
    sfdipot_areas: list[str],
    base_urls: list[str],
    env_var_names: list[str],
    readme: str | None,
    file_tree: str | None,
    tools: dict[str, Callable[..., str]],
    max_actions: int,
    snapshot_window: int,
    on_round: Callable[[], None],
    secret_values: set[str] | None = None,
) -> ExplorationLoopResult:
    """Drive one charter's session as a bounded browser tool loop.

    A sibling of ``_complete_with_tools`` rather than a generalization of it:
    the two differ in termination (a terminal ``finish_session`` tool, not
    "stopped calling tools"), in memory management (snapshot pruning, which no
    other caller needs), and in output (a forced structured wrap-up).  What is
    shared — the client, JSON parsing, and sending ``tools`` together with
    ``response_format`` — is reused directly; see ``_complete_with_tools`` for
    why that combination is required against DeepSeek.

    ``tools`` maps tool name to executor.  Executors must never raise: they
    return error strings the model can react to, the same contract as
    ``read_file``.

    ``secret_values`` is a redaction backstop for the action log: ``fill_secret``
    already keeps credentials out of this module entirely, so this only catches
    a model that typed a literal through plain ``fill``.
    """
    client = _get_client()
    parts = exploration_context(
        name, description, charter, sfdipot_areas, base_urls, env_var_names, readme, file_tree
    )
    user_prompt = (
        "\n\n".join(parts)
        + f"\n\nYou have {max_actions} actions for this session. Use them deliberately."
    )
    messages: list = [
        {"role": "system", "content": EXPLORATION_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    action_log: list[str] = []
    snapshot_indices: list[int] = []
    last_signature: str | None = None
    repeat_count = 0
    actions_used = 0

    while actions_used < max_actions:
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=BROWSER_TOOLS,
                response_format={"type": "json_object"},
            )
        except openai.OpenAIError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        on_round()
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)

        if not tool_calls:
            # The model answered instead of acting — it has decided it is done.
            # Every round is sent with response_format=json_object, so that
            # answer is a JSON object rather than prose; parse it like the
            # wrap-up call does, and keep the raw text if it isn't the
            # expected shape so nothing the model said is ever lost.
            try:
                notes = _parse_json(message.content, SessionWrapUpResult).notes.strip()
            except LLMError:
                notes = (message.content or "").strip()
            return ExplorationLoopResult(
                notes=notes or "(model ended the session without notes)",
                stop_reason=STOP_CHARTER_COMPLETE,
                actions_used=actions_used,
                action_log=action_log,
            )

        messages.append(message)

        for tool_call in tool_calls:
            tool_name = tool_call.function.name
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}

            if tool_name == "finish_session":
                notes = str(arguments.get("notes", "")).strip()
                action_log.append("finish_session()")
                return ExplorationLoopResult(
                    notes=notes or "(session finished without notes)",
                    stop_reason=STOP_CHARTER_COMPLETE,
                    actions_used=actions_used,
                    action_log=action_log,
                )

            actions_used += 1
            signature = f"{tool_name}:{sorted(arguments.items())}"
            if signature == last_signature:
                repeat_count += 1
            else:
                repeat_count = 0
                last_signature = signature

            if repeat_count >= _REPEAT_NUDGE_THRESHOLD:
                # Don't re-execute an action that has already produced the
                # same result three times — nudge instead, and still charge
                # it against the budget so a stuck model cannot loop forever.
                result = (
                    "You have repeated this exact action several times with the "
                    "same result. Try something different, or call "
                    "finish_session if the charter is explored."
                )
            else:
                executor = tools.get(tool_name)
                if executor is None:
                    result = f"ERROR: unknown tool {tool_name!r}."
                else:
                    result = executor(**arguments)

            action_log.append(
                f"{tool_name}({_format_args(arguments, secret_values)}) -> {_summarize(result)}"
            )
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result})
            if tool_name == "snapshot":
                snapshot_indices.append(len(messages) - 1)
                _prune_snapshots(messages, snapshot_indices, snapshot_window)

        remaining = max_actions - actions_used
        messages[-1]["content"] += (
            f"\n[{remaining} of {max_actions} actions remaining — "
            "call finish_session when the charter is explored]"
        )

    # Budget exhausted — force the session notes.
    messages.append({"role": "user", "content": SESSION_WRAPUP_PROMPT})
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            tools=BROWSER_TOOLS,
            tool_choice="none",
            response_format={"type": "json_object"},
        )
    except openai.OpenAIError as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc
    on_round()
    wrap_up = _parse_json(response.choices[0].message.content, SessionWrapUpResult)
    return ExplorationLoopResult(
        notes=wrap_up.notes,
        stop_reason=STOP_ACTION_CAP,
        actions_used=actions_used,
        action_log=action_log,
    )


def _format_args(arguments: dict, secret_values: set[str] | None = None) -> str:
    """Render tool arguments for the action log.

    ``fill_secret`` is logged by variable name only — its value is resolved
    inside the executor and never reaches this module, which is what keeps
    the stored log credential-free by construction.

    ``secret_values`` is the backstop for the one path that bypasses it: a
    model that ignores the instruction and types a credential through plain
    ``fill``.  Matching is **exact**, not substring — the model never sees
    environment values, so a leak means it reproduced one verbatim, whereas
    substring matching would mangle ordinary log lines that happen to contain
    a short value.
    """
    secrets = secret_values or set()
    return ", ".join(
        f"{key}={'***' if isinstance(value, str) and value in secrets else repr(value)}"
        for key, value in arguments.items()
    )


def _summarize(result: str, limit: int = 200) -> str:
    """Condense a tool result for the action log (snapshots are huge)."""
    collapsed = " ".join(result.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"
