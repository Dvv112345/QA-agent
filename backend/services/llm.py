"""LLM client for requirement clarity analysis.

Talks to any OpenAI-compatible API (DeepSeek by default via
``OPENAI_BASE_URL``) using the sync OpenAI SDK — the callers are RQ worker
tasks, not request handlers.  JSON output is requested with the portable
``json_object`` response format plus explicit shape instructions in the
prompt, then validated with a pydantic model; anything that goes wrong
surfaces as ``LLMError``.

The API key is never logged and prompts are never logged at INFO level.
"""

from __future__ import annotations

import json
import logging
import ssl

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
    clarifying_questions: str | None = None
    rewritten_description: str | None = None


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


_CHECK_SYSTEM_PROMPT = (
    "You are a senior QA engineer reviewing software requirements. "
    "Judge whether the given requirement is clear and specific enough to write "
    "test cases against. If it is not, ask clarifying questions"
    "Respond with a JSON object of the shape "
    '{"clear": boolean, "clarifying_questions": string or null}.'
)

_REVISE_SYSTEM_PROMPT = (
    "You are a senior QA engineer refining software requirements. "
    "Rewrite the requirement description so it incorporates the user's answer "
    "to your clarifying question, keeping the user's intent. Then judge whether "
    "the rewritten requirement is clear and specific enough to write test cases "
    "against; if not, ask new clarifying questions. "
    "Respond with a JSON object of the shape "
    '{"clear": boolean, "clarifying_questions": string or null, '
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


def _complete(system_prompt: str, user_prompt: str) -> ClarityResult:
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
        result = ClarityResult.model_validate(json.loads(content or ""))
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMError(f"LLM returned malformed output: {exc}") from exc

    if not result.clear and not result.clarifying_questions:
        raise LLMError("LLM judged the requirement unclear but gave no clarifying question.")
    return result


def check_clarity(
    name: str,
    description: str,
    readme: str | None,
    file_tree: str | None,
) -> ClarityResult:
    """Judge whether a requirement is clear enough to write tests against."""
    parts = _context_sections(readme, file_tree)
    parts.append(f"Requirement name: {name}\nRequirement description:\n{description}")
    return _complete(_CHECK_SYSTEM_PROMPT, "\n\n".join(parts))


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
    result = _complete(_REVISE_SYSTEM_PROMPT, "\n\n".join(parts))
    if not result.rewritten_description:
        raise LLMError("LLM revision did not include a rewritten description.")
    return result
