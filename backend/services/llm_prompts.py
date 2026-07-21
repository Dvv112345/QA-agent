"""System prompts, prompt-assembly helpers, and tool schemas for ``services/llm.py``.

Kept separate from the API-calling mechanics so prompt text can be found and
edited without wading through client/completion plumbing.
"""

from __future__ import annotations

from backend.config import README_MAX_CHARS


def context_sections(readme: str | None, file_tree: str | None) -> list[str]:
    """Build optional project-context blocks for the user prompt."""
    sections: list[str] = []
    if readme:
        sections.append(f"Project README:\n---\n{readme[:README_MAX_CHARS]}\n---")
    if file_tree:
        sections.append(f"Repository file tree:\n---\n{file_tree}\n---")
    return sections


# ── Requirement clarity ──────────────────────────────────────────────

CLARITY_BAR = (
    "A requirement is clear if a competent QA engineer could write "
    "meaningful test cases from it without guessing at the author's intent — it "
    "does not need to cover every edge case, exact error message, or precise UI "
    "copy; those are normal test-design decisions, not blockers. Use the "
    "provided README and file tree to resolve ambiguity yourself before asking "
    "the user. "
)

CHECK_SYSTEM_PROMPT = (
    "You are a senior QA engineer reviewing software requirements. "
    f"{CLARITY_BAR} If genuinely unclear, ask about the gaps that actually "
    "block writing a test case — bundle multiple questions into "
    "clarifying_question if needed, but skip nice-to-know details. "
    "Respond with a JSON object of the shape "
    '{"clear": boolean, "clarifying_question": string or null}.'
)

REVISE_SYSTEM_PROMPT = (
    "You are a senior QA engineer refining software requirements. "
    "Rewrite the requirement description so it incorporates the user's answer "
    "to your clarifying question, keeping the user's intent. Then judge whether "
    f"the rewritten requirement is clear enough to write test cases against — "
    f"{CLARITY_BAR} If still genuinely unclear, ask new clarifying questions "
    "about the gaps that actually block writing a test case; bundle multiple "
    "questions together if needed, but skip nice-to-know details. "
    "Respond with a JSON object of the shape "
    '{"clear": boolean, "clarifying_question": string or null, '
    '"rewritten_description": string}.'
)


# ── PRD splitting ─────────────────────────────────────────────────────

SPLIT_PRD_SYSTEM_PROMPT = (
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


# ── Test environment access ───────────────────────────────────────────

TEST_ENV_BAR = (
    "The description is sufficient if a competent QA engineer could reach and "
    "exercise every service the confirmed requirements touch without guessing. "
    "This includes access required for seeding data needed by test cases. "
    "For each such service it must say how to access it (URL, host, or entry "
    "point) and what credentials to use or how to obtain them. It does not "
    "need deployment internals or exhaustive tooling detail. Use the provided "
    "requirements, README, and file tree to resolve ambiguity yourself before "
    "asking the user. "
)

TEST_ENV_CHECK_SYSTEM_PROMPT = (
    "You are a senior QA engineer reviewing a description of how to access a "
    f"test environment. {TEST_ENV_BAR} If genuinely insufficient, ask about "
    "the gaps that actually block reaching the services under test — bundle "
    "multiple questions into clarifying_question if needed, but skip "
    "nice-to-know details. Respond with a JSON object of the shape "
    '{"sufficient": boolean, "clarifying_question": string or null}.'
)

TEST_ENV_REVISE_SYSTEM_PROMPT = (
    "You are a senior QA engineer refining a description of how to access a "
    "test environment. Rewrite the description so it incorporates the user's "
    "answer to your clarifying question, keeping the user's intent. Then "
    f"judge whether the rewritten description is sufficient — {TEST_ENV_BAR} "
    "If still genuinely insufficient, ask new clarifying questions about the "
    "gaps that actually block reaching the services under test; bundle "
    "multiple questions together if needed, but skip nice-to-know details. "
    "Respond with a JSON object of the shape "
    '{"sufficient": boolean, "clarifying_question": string or null, '
    '"rewritten_content": string}.'
)


def requirements_section(requirements: list[tuple[str, str]]) -> str:
    """Format the confirmed requirements as a context block."""
    blocks = [f"- {name}: {description}" for name, description in requirements]
    return "Confirmed requirements to be tested:\n---\n" + "\n".join(blocks) + "\n---"


# ── Test plans ────────────────────────────────────────────────────────

# OpenAI function schema for the repo file-reading tool offered to the model.
READ_FILE_TOOL = {
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


TEST_PLAN_BAR = (
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

TEST_PLAN_JSON_SHAPE = (
    "Respond with ONLY a JSON object of the shape "
    '{"complexity": "low"|"medium"|"high", "summary": string, '
    '"cases": [{"title": string, "preconditions": string or null, '
    '"steps": [string], "expected_result": string, "case_type": string, '
    '"priority": "high"|"medium"|"low"}]}.'
)

TEST_PLAN_SYSTEM_PROMPT = (
    "You are a senior QA engineer writing a test plan for a single software "
    f"requirement. {TEST_PLAN_BAR}{TEST_PLAN_JSON_SHAPE}"
)

TEST_PLAN_REVISE_SYSTEM_PROMPT = (
    "You are a senior QA engineer revising a test plan for a single software "
    "requirement according to the user's feedback. Produce the full revised "
    "plan — repeat unchanged cases verbatim. "
    f"{TEST_PLAN_BAR}{TEST_PLAN_JSON_SHAPE}"
)


def test_plan_context(
    name: str,
    description: str,
    sibling_names: list[str],
    test_env_content: str | None,
    readme: str | None,
    file_tree: str | None,
) -> list[str]:
    """Shared user-prompt blocks for generate and revise."""
    parts = context_sections(readme, file_tree)
    if test_env_content:
        parts.append(f"Test environment access:\n---\n{test_env_content}\n---")
    if sibling_names:
        parts.append(
            "Other requirements in this sprint (scope boundaries — do not "
            "write test cases for them):\n" + "\n".join(f"- {sibling}" for sibling in sibling_names)
        )
    parts.append(f"Requirement name: {name}\nRequirement description:\n{description}")
    return parts
