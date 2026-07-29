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
    EXPLORATORY_CONTEXT_TOKEN_LIMIT,
    EXPLORATORY_MAX_CHARTERS,
    EXPLORATORY_MAX_FINDINGS,
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
    HISTORY_COMPACTION_PROMPT,
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
    """The model's closing notes.

    Deliberately notes-only: the stop reason is decided here, from which exit
    the loop actually took, so asking the model for one would pay tokens for
    an answer that is always discarded.
    """

    notes: str


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
# The model stopped calling tools without calling finish_session. Distinct
# from STOP_CHARTER_COMPLETE so a session that died on round one is visibly
# different from one that finished its charter — they were indistinguishable
# before, which is what let the JSON-mode bug below go unnoticed.
STOP_MODEL_STOPPED = "model_stopped"

# The session ran out of context room before it ran out of actions.
STOP_CONTEXT_LIMIT = "context_limit"

# Never compacted: the system prompt and the opening user message carrying the
# requirement, charter, and base URLs. That is the session's oracle — losing it
# would be far worse than an overflow.
_COMPACTION_PRESERVED_HEAD = 2

# Below this many messages there is nothing worth an LLM call, and retrying
# every round would thrash.
_MIN_COMPACTION_MESSAGES = 4

# Remaining actions at which the budget note switches from "wrap up soon" to
# "record now or lose it" — see the call site for why the two differ.
_LOW_BUDGET_ACTIONS = 3

# Consecutive tool-free responses tolerated before the session really ends.
# One is not a decision: a model that means to act can express the call as
# prose or as a JSON blob instead of using the tool channel, and ending the
# session there costs the whole charter. Nudge first, end if it repeats.
_MAX_IDLE_ROUNDS = 2

_ACT_OR_FINISH_NUDGE = (
    "That response did not call a tool, so nothing happened. Use the tool "
    "interface to act — do not describe a tool call in your message text. If "
    "the charter is fully explored, call finish_session with your notes."
)

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


def _is_tool_result(message) -> bool:
    return isinstance(message, dict) and message.get("role") == "tool"


def _message_text(message) -> str:
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def _estimate_tokens(messages: list) -> int:
    """Rough prompt size, used only when the provider reports no usage.

    Four characters per token is the usual English approximation; ARIA
    snapshots are denser than prose so this under-counts, which is the safe
    direction for a fallback that only has to decide "are we near the limit".
    """
    return sum(len(_message_text(m)) for m in messages) // 4


def _compaction_cut(messages: list, keep_groups: int) -> int:
    """Index where the retained tail starts, or 0 when there is nothing to compact.

    A ``role="tool"`` message is only valid immediately after the assistant
    message carrying its ``tool_call_id``, so a cut landing mid-group would
    orphan tool results and the provider rejects the entire request.  Every
    non-tool-result message is therefore a safe boundary — note that includes
    the idle-nudge pair (an assistant message with no ``tool_calls``, then a
    user message), which is why this tests for "not a tool result" rather than
    "assistant with tool calls".

    Walks back from the end counting boundaries, so the removed span is whole
    groups by construction.
    """
    boundaries_seen = 0
    for index in range(len(messages) - 1, _COMPACTION_PRESERVED_HEAD - 1, -1):
        if _is_tool_result(messages[index]):
            continue
        boundaries_seen += 1
        if boundaries_seen > keep_groups:
            return index
    return 0


def _render_history(messages: list) -> str:
    """Flatten a span of the conversation into text for the compaction call."""
    lines: list[str] = []
    for message in messages:
        if _is_tool_result(message):
            lines.append(f"RESULT: {_message_text(message)}")
            continue
        if isinstance(message, dict):
            lines.append(f"{message.get('role', 'user').upper()}: {_message_text(message)}")
            continue
        text = _message_text(message)
        if text:
            lines.append(f"TESTER: {text}")
        for call in getattr(message, "tool_calls", None) or []:
            lines.append(f"ACTION: {call.function.name}({call.function.arguments})")
    return "\n".join(lines)


def _compact_history(messages: list, keep_groups: int) -> bool:
    """Replace the middle of the conversation with an LLM summary, in place.

    Returns ``False`` when there is nothing worth compacting — which, while
    over the token limit, means the floor itself (system prompt + charter +
    the verbatim snapshot window) exceeds it and no amount of retrying will
    help.  Raises ``LLMError`` if the summarising call fails.
    """
    cut = _compaction_cut(messages, keep_groups)
    span = messages[_COMPACTION_PRESERVED_HEAD:cut]
    if len(span) < _MIN_COMPACTION_MESSAGES:
        return False

    result = _complete(HISTORY_COMPACTION_PROMPT, _render_history(span), ExplorationSummaryResult)
    messages[_COMPACTION_PRESERVED_HEAD:cut] = [
        {
            "role": "user",
            "content": (
                "[Earlier in this session, compacted to save context. Element "
                "refs from before this point are stale — take a fresh "
                f"snapshot before acting.]\n{result.summary}"
            ),
        }
    ]
    return True


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
    max_free_recordings: int = EXPLORATORY_MAX_FINDINGS,
    context_token_limit: int = EXPLORATORY_CONTEXT_TOKEN_LIMIT,
) -> ExplorationLoopResult:
    """Drive one charter's session as a bounded browser tool loop.

    A sibling of ``_complete_with_tools`` rather than a generalization of it:
    the two differ in termination (a terminal ``finish_session`` tool, not
    "stopped calling tools"), in memory management (snapshot pruning, which no
    other caller needs), and in output (a forced structured wrap-up).

    That difference in termination is why this loop must **not** copy
    ``_complete_with_tools``'s ``response_format={"type": "json_object"}`` onto
    its acting rounds, even though both send ``tools``.  There, JSON is the
    wanted final answer and "stopped calling tools" means "answered"; here it
    means "session over", so nudging the model toward content output is
    actively harmful — it ended sessions at zero actions.  JSON mode survives
    only on the forced wrap-up call at the end.

    ``tools`` maps tool name to executor.  Executors must never raise: they
    return error strings the model can react to, the same contract as
    ``read_file``.

    ``secret_values`` is a redaction backstop for the action log: ``fill_secret``
    already keeps credentials out of this module entirely, so this only catches
    a model that typed a literal through plain ``fill``.

    ``record_finding`` is free for the first ``max_free_recordings`` calls:
    findings are the session's whole deliverable, and charging them against
    the same budget as exploration makes the model trade away its own output
    under budget pressure.  It stops being free after that — ``actions_used <
    max_actions`` is this loop's only bound, so an unconditionally free
    non-terminal tool would remove the termination guarantee entirely (unlike
    ``finish_session``, which is free *and* terminal and therefore cannot
    loop).  Total rounds are thus capped at
    ``max_actions + max_free_recordings``.
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
    # Snapshot results are identified by tool_call_id rather than by position,
    # because compaction shifts every index after the span it removes.
    snapshot_call_ids: set[str] = set()
    last_signature: str | None = None
    repeat_count = 0
    actions_used = 0
    idle_rounds = 0
    free_recordings_left = max_free_recordings

    while actions_used < max_actions:
        try:
            # Deliberately NO response_format here, unlike every other call in
            # this module. Acting rounds want a *tool call*, and JSON mode
            # tells the model its output must be a JSON object — the shape of
            # "emit content". Observed against DeepSeek: the model answered
            # with {"tool": "snapshot", "params": {}} as message content
            # instead of calling the tool, which ended sessions at zero
            # actions. The forced wrap-up below still uses JSON mode, because
            # there the JSON object genuinely is the wanted output.
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=BROWSER_TOOLS,
            )
        except openai.OpenAIError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        on_round()
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)

        if not tool_calls:
            idle_rounds += 1
            if idle_rounds < _MAX_IDLE_ROUNDS:
                # Not necessarily "done" — more often a model that meant to
                # act but wrote the call out instead of using the tool
                # channel. Point it back at the tools rather than throwing
                # away the charter.
                messages.append(message)
                messages.append({"role": "user", "content": _ACT_OR_FINISH_NUDGE})
                continue
            # Twice in a row: take it at its word. Parse as a wrap-up when it
            # fits, else keep the raw text so nothing the model said is lost.
            try:
                notes = _parse_json(message.content, SessionWrapUpResult).notes.strip()
            except LLMError:
                notes = (message.content or "").strip()
            return ExplorationLoopResult(
                notes=notes or "(model ended the session without notes)",
                stop_reason=STOP_MODEL_STOPPED,
                actions_used=actions_used,
                action_log=action_log,
            )

        idle_rounds = 0
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

            # Recording what you found must not compete with exploring for
            # budget. Free only while it can still succeed: past the cap the
            # executor just returns "limit reached", and a free call that
            # changes nothing is how a stuck model loops forever.
            if tool_name == "record_finding" and free_recordings_left > 0:
                free_recordings_left -= 1
            else:
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
                snapshot_call_ids.add(tool_call.id)
                snapshot_indices.append(len(messages) - 1)
                _prune_snapshots(messages, snapshot_indices, snapshot_window)

        remaining = max_actions - actions_used
        # Near the cap the advice changes: the forced wrap-up runs with
        # tool_choice="none", so record_finding is genuinely unreachable once
        # the budget is gone. A finding still unrecorded at that point can only
        # ever land in the notes, where nothing reads it as a finding.
        if remaining <= _LOW_BUDGET_ACTIONS:
            tail = (
                "record anything you have found but not yet recorded — after "
                "your last action you can only write notes, not findings"
            )
        else:
            tail = "call finish_session when the charter is explored"
        messages[-1]["content"] += f"\n[{remaining} of {max_actions} actions remaining — {tail}]"

        # Context backstop. Measured from what the request we just made
        # actually cost, so it reacts a round late — fine for something that
        # normally never fires, and it avoids estimating on every round.
        used_tokens = getattr(getattr(response, "usage", None), "prompt_tokens", None)
        if used_tokens is None:
            used_tokens = _estimate_tokens(messages)
        if used_tokens > context_token_limit:
            try:
                # Keep every snapshot the pruner still holds verbatim, plus the
                # round that produced the oldest of them: compacting one away
                # would strip the refs the model is about to act on.
                compacted = _compact_history(messages, snapshot_window + 1)
            except LLMError as exc:
                logger.warning("Exploration history compaction failed: %s", exc)
                return _forced_wrap_up(
                    client, messages, on_round, STOP_CONTEXT_LIMIT, actions_used, action_log
                )
            on_round()
            if not compacted:
                # Nothing left to compact while still over the limit: the
                # floor exceeds it, so retrying next round would only thrash.
                logger.warning("Exploration context over limit with nothing left to compact")
                return _forced_wrap_up(
                    client, messages, on_round, STOP_CONTEXT_LIMIT, actions_used, action_log
                )
            snapshot_indices = [
                index
                for index, item in enumerate(messages)
                if _is_tool_result(item) and item.get("tool_call_id") in snapshot_call_ids
            ]

    return _forced_wrap_up(client, messages, on_round, STOP_ACTION_CAP, actions_used, action_log)


def _forced_wrap_up(
    client,
    messages: list,
    on_round: Callable[[], None],
    stop_reason: str,
    actions_used: int,
    action_log: list[str],
) -> ExplorationLoopResult:
    """Ask for the session notes and stop, however the session ran out.

    Shared by the action cap and both context-limit exits.  Keeps JSON mode —
    unlike the acting rounds, here the JSON object genuinely is the wanted
    output, and ``SESSION_WRAPUP_PROMPT`` asks for it.
    """
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
        stop_reason=stop_reason,
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
