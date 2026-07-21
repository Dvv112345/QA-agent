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
from typing import Literal, TypeVar

import certifi
import httpx
import openai
from sqlmodel import SQLModel

from backend.config import (
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    OPENAI_TIMEOUT,
    TEST_PLAN_TOOL_ROUNDS,
)
from backend.models.database import TestCasePriority
from backend.services.llm_prompts import (
    CHECK_SYSTEM_PROMPT,
    READ_FILE_TOOL,
    REVISE_SYSTEM_PROMPT,
    SPLIT_PRD_SYSTEM_PROMPT,
    TEST_ENV_CHECK_SYSTEM_PROMPT,
    TEST_ENV_REVISE_SYSTEM_PROMPT,
    TEST_PLAN_REVISE_SYSTEM_PROMPT,
    TEST_PLAN_SYSTEM_PROMPT,
    context_sections,
    requirements_section,
    test_plan_context,
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


def _parse_test_plan(content: str | None) -> TestPlanResult:
    try:
        return TestPlanResult.model_validate(json.loads(content or ""))
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMError(f"LLM returned malformed output: {exc}") from exc


def _complete_with_tools(
    system_prompt: str,
    user_prompt: str,
    read_file: Callable[[str], str] | None,
    on_round: Callable[[], None],
) -> TestPlanResult:
    """Run a bounded read_file tool loop and parse the final JSON plan.

    Every round sends ``tools`` together with strict JSON mode — verified
    against DeepSeek (2026-07-16): the combination works, and omitting
    ``response_format`` yields unparseable (fenced) final answers.  After
    ``TEST_PLAN_TOOL_ROUNDS`` tool rounds, one final call is forced with
    ``tool_choice="none"``.  ``read_file`` never raises — it returns error
    strings the model can react to.  With ``read_file=None`` (no file tree)
    this degrades to a plain completion.
    """
    if read_file is None:
        return _complete(system_prompt, user_prompt, TestPlanResult)

    client = _get_client()
    budget_prompt = (
        f"{user_prompt}\n\n"
        f"You may use up to {TEST_PLAN_TOOL_ROUNDS} rounds of read_file calls "
        "before you must answer with the JSON plan."
    )
    messages: list = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": budget_prompt},
    ]

    for round_no in range(1, TEST_PLAN_TOOL_ROUNDS + 1):
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
                    "LLM provider rejected tool calls; falling back to "
                    "context-only test plan generation: %s",
                    exc,
                )
                return _complete(system_prompt, user_prompt, TestPlanResult)
            raise LLMError(f"LLM request failed: {exc}") from exc
        except openai.OpenAIError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        on_round()
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            return _parse_test_plan(message.content)

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
        remaining = TEST_PLAN_TOOL_ROUNDS - round_no
        messages[-1]["content"] += (
            f"\n[read_file budget: {remaining} of {TEST_PLAN_TOOL_ROUNDS} rounds "
            "remaining — respond with the JSON plan when you have enough context]"
        )

    # Round cap hit — force the final answer.
    messages.append(
        {
            "role": "user",
            "content": (
                "Your read_file budget is exhausted. Respond now with only the JSON test plan."
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
    return _parse_test_plan(response.choices[0].message.content)


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
    result = _complete_with_tools(TEST_PLAN_SYSTEM_PROMPT, "\n\n".join(parts), read_file, on_round)
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
        TEST_PLAN_REVISE_SYSTEM_PROMPT, "\n\n".join(parts), read_file, on_round
    )
    return _validate_test_plan(result)
