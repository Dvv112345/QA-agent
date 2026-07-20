"""LLM client for requirement clarity and test-environment analysis.

Talks to any OpenAI-compatible API (DeepSeek by default via
``OPENAI_BASE_URL``) using the sync OpenAI SDK — the callers are RQ worker
tasks or routes that offload to a thread (``asyncio.to_thread``).  JSON
output is requested with the portable ``json_object`` response format plus
explicit shape instructions in the prompt, then validated with a pydantic
model; anything that goes wrong surfaces as ``LLMError``.

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

# On Windows, SSL_CERT_FILE may point to a non-existent file which breaks
# httpx's default SSL context; use certifi like github_utils does.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger(__name__)

# READMEs can be arbitrarily long; cap what we spend of the prompt budget.
README_MAX_CHARS = 8000


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


_CLARITY_BAR = (
    "A requirement is clear if a competent QA engineer could write "
    "meaningful test cases from it without guessing at the author's intent — it "
    "does not need to cover every edge case, exact error message, or precise UI "
    "copy; those are normal test-design decisions, not blockers. Use the "
    "provided README and file tree to resolve ambiguity yourself before asking "
    "the user. "
)

_CHECK_SYSTEM_PROMPT = (
    "You are a senior QA engineer reviewing software requirements. "
    f"{_CLARITY_BAR} If genuinely unclear, ask about the gaps that actually "
    "block writing a test case — bundle multiple questions into "
    "clarifying_question if needed, but skip nice-to-know details. "
    "Respond with a JSON object of the shape "
    '{"clear": boolean, "clarifying_question": string or null}.'
)

_REVISE_SYSTEM_PROMPT = (
    "You are a senior QA engineer refining software requirements. "
    "Rewrite the requirement description so it incorporates the user's answer "
    "to your clarifying question, keeping the user's intent. Then judge whether "
    f"the rewritten requirement is clear enough to write test cases against — "
    f"{_CLARITY_BAR} If still genuinely unclear, ask new clarifying questions "
    "about the gaps that actually block writing a test case; bundle multiple "
    "questions together if needed, but skip nice-to-know details. "
    "Respond with a JSON object of the shape "
    '{"clear": boolean, "clarifying_question": string or null, '
    '"rewritten_description": string}.'
)


def _context_sections(readme: str | None, file_tree: str | None) -> list[str]:
    """Build optional project-context blocks for the user prompt."""
    sections: list[str] = []
    if readme:
        sections.append(f"Project README:\n---\n{readme[:README_MAX_CHARS]}\n---")
    if file_tree:
        sections.append(f"Repository file tree:\n---\n{file_tree}\n---")
    return sections


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
    parts = _context_sections(readme, file_tree)
    parts.append(f"Requirement name: {name}\nRequirement description:\n{description}")
    result = _complete(_CHECK_SYSTEM_PROMPT, "\n\n".join(parts), ClarityResult)
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
    parts = _context_sections(readme, file_tree)
    parts.append(
        f"Requirement name: {name}\n"
        f"Current requirement description:\n{description}\n\n"
        f"Clarifying question that was asked:\n{question}\n\n"
        f"User's answer:\n{answer}"
    )
    result = _complete(_REVISE_SYSTEM_PROMPT, "\n\n".join(parts), ClarityResult)
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


_SPLIT_PRD_SYSTEM_PROMPT = (
    "You are a senior QA engineer turning a product requirements document "
    "(PRD) into discrete software requirements to be tested. Split the "
    "document into separate requirements: each gets a short name and a "
    "self-contained description that makes sense without reading the rest "
    "of the document, because each requirement is reviewed in isolation "
    "later. Cover every requirement the document states, but do not invent "
    "requirements that are not in it, and do not merge unrelated features "
    "into one requirement. Respond with a JSON object of the shape "
    '{"requirements": [{"name": string, "description": string}]}. '
    "Return an empty list if the document contains no software requirements."
)


def split_prd(prd_text: str, readme: str | None, file_tree: str | None) -> PrdSplitResult:
    """Split an uploaded PRD document into discrete requirements.

    Returns an *empty* result when the model finds no requirements — the
    caller decides how to report that to the user.  A partially empty item
    (name without description or vice versa) is malformed output.
    """
    parts = _context_sections(readme, file_tree)
    parts.append(f"PRD document:\n---\n{prd_text}\n---")
    result = _complete(_SPLIT_PRD_SYSTEM_PROMPT, "\n\n".join(parts), PrdSplitResult)

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

_TEST_ENV_BAR = (
    "The description is sufficient if a competent QA engineer could reach and "
    "exercise every service the confirmed requirements touch without guessing: "
    "for each such service it must say how to access it (URL, host, or entry "
    "point) and what credentials to use or how to obtain them. It does not "
    "need deployment internals or exhaustive tooling detail. Use the provided "
    "requirements, README, and file tree to resolve ambiguity yourself before "
    "asking the user. "
)

_TEST_ENV_CHECK_SYSTEM_PROMPT = (
    "You are a senior QA engineer reviewing a description of how to access a "
    f"test environment. {_TEST_ENV_BAR} If genuinely insufficient, ask about "
    "the gaps that actually block reaching the services under test — bundle "
    "multiple questions into clarifying_question if needed, but skip "
    "nice-to-know details. Respond with a JSON object of the shape "
    '{"sufficient": boolean, "clarifying_question": string or null}.'
)

_TEST_ENV_REVISE_SYSTEM_PROMPT = (
    "You are a senior QA engineer refining a description of how to access a "
    "test environment. Rewrite the description so it incorporates the user's "
    "answer to your clarifying question, keeping the user's intent. Then "
    f"judge whether the rewritten description is sufficient — {_TEST_ENV_BAR} "
    "If still genuinely insufficient, ask new clarifying questions about the "
    "gaps that actually block reaching the services under test; bundle "
    "multiple questions together if needed, but skip nice-to-know details. "
    "Respond with a JSON object of the shape "
    '{"sufficient": boolean, "clarifying_question": string or null, '
    '"rewritten_content": string}.'
)


def _requirements_section(requirements: list[tuple[str, str]]) -> str:
    """Format the confirmed requirements as a context block."""
    blocks = [f"- {name}: {description}" for name, description in requirements]
    return "Confirmed requirements to be tested:\n---\n" + "\n".join(blocks) + "\n---"


def check_test_environment(
    content: str,
    requirements: list[tuple[str, str]],
    readme: str | None,
    file_tree: str | None,
) -> TestEnvironmentResult:
    """Judge whether a test-environment access description is sufficient."""
    parts = [_requirements_section(requirements)]
    parts.extend(_context_sections(readme, file_tree))
    parts.append(f"Test environment access description:\n{content}")
    result = _complete(_TEST_ENV_CHECK_SYSTEM_PROMPT, "\n\n".join(parts), TestEnvironmentResult)
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
    parts = [_requirements_section(requirements)]
    parts.extend(_context_sections(readme, file_tree))
    parts.append(
        f"Current test environment access description:\n{content}\n\n"
        f"Clarifying question that was asked:\n{question}\n\n"
        f"User's answer:\n{answer}"
    )
    result = _complete(_TEST_ENV_REVISE_SYSTEM_PROMPT, "\n\n".join(parts), TestEnvironmentResult)
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


# OpenAI function schema for the repo file-reading tool offered to the model.
_READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a file from the repository under test. The path must be one "
            "of the paths listed in the provided repository file tree."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative file path."}
            },
            "required": ["path"],
        },
    },
}


_TEST_PLAN_BAR = (
    "Rate the requirement's testing complexity as low, medium, or high and "
    "scale the plan accordingly: a trivial requirement needs only a few "
    "focused checks, while a complex one needs thorough coverage including "
    "edge and negative cases. Write steps a QA engineer can execute "
    "concretely against the described test environment. The other "
    "requirements listed are scope boundaries only — do not write test "
    "cases for them. Use the read_file tool with paths taken from the "
    "provided file tree to ground routes, parameters, and validation rules "
    "in the real code before finalizing. "
)

_TEST_PLAN_JSON_SHAPE = (
    "Respond with ONLY a JSON object of the shape "
    '{"complexity": "low"|"medium"|"high", "summary": string, '
    '"cases": [{"title": string, "preconditions": string or null, '
    '"steps": [string], "expected_result": string, "case_type": string, '
    '"priority": "high"|"medium"|"low"}]}.'
)

_TEST_PLAN_SYSTEM_PROMPT = (
    "You are a senior QA engineer writing a test plan for a single software "
    f"requirement. {_TEST_PLAN_BAR}{_TEST_PLAN_JSON_SHAPE}"
)

_TEST_PLAN_REVISE_SYSTEM_PROMPT = (
    "You are a senior QA engineer revising a test plan for a single software "
    "requirement according to the user's feedback. Produce the full revised "
    "plan — repeat unchanged cases verbatim. "
    f"{_TEST_PLAN_BAR}{_TEST_PLAN_JSON_SHAPE}"
)


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
                tools=[_READ_FILE_TOOL],
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
            tools=[_READ_FILE_TOOL],
            tool_choice="none",
            response_format={"type": "json_object"},
        )
    except openai.OpenAIError as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc
    on_round()
    return _parse_test_plan(response.choices[0].message.content)


def _test_plan_context(
    name: str,
    description: str,
    sibling_names: list[str],
    test_env_content: str | None,
    readme: str | None,
    file_tree: str | None,
) -> list[str]:
    """Shared user-prompt blocks for generate and revise."""
    parts = _context_sections(readme, file_tree)
    if test_env_content:
        parts.append(f"Test environment access:\n---\n{test_env_content}\n---")
    if sibling_names:
        parts.append(
            "Other requirements in this sprint (scope boundaries — do not "
            "write test cases for them):\n" + "\n".join(f"- {sibling}" for sibling in sibling_names)
        )
    parts.append(f"Requirement name: {name}\nRequirement description:\n{description}")
    return parts


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
    parts = _test_plan_context(
        name, description, sibling_names, test_env_content, readme, file_tree
    )
    result = _complete_with_tools(_TEST_PLAN_SYSTEM_PROMPT, "\n\n".join(parts), read_file, on_round)
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
    parts = _test_plan_context(
        name, description, sibling_names, test_env_content, readme, file_tree
    )
    parts.append(f"Current test plan (JSON):\n{current_plan_json}\n\nUser's feedback:\n{feedback}")
    result = _complete_with_tools(
        _TEST_PLAN_REVISE_SYSTEM_PROMPT, "\n\n".join(parts), read_file, on_round
    )
    return _validate_test_plan(result)
