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
from typing import TypeVar

import certifi
import httpx
import openai
from sqlmodel import SQLModel

from backend.config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, OPENAI_TIMEOUT

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
