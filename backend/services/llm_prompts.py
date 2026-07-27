"""System prompts, prompt-assembly helpers, and tool schemas for ``services/llm.py``.

Kept separate from the API-calling mechanics so prompt text can be found and
edited without wading through client/completion plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    "into one requirement. Do not divide a single requirement into multiple"
    "smaller requirements. Respond with a JSON object of the shape "
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
    "asking the user. Note that the tests will not be running from the same "
    "directory as the codebase, so relative path should not be accepted."
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
    "edge and negative cases. Base every case's steps and expected result on "
    "what the requirement itself says should happen — the requirement is the "
    "source of truth, not the current implementation. Do not base expected "
    "result on the code returned by read_file. Write steps a QA engineer can execute "
    "concretely against the described test environment. The other "
    "requirements listed are scope boundaries only — do not write test "
    "cases for them. Use the read_file tool with paths taken from the "
    "provided file tree only to confirm the real endpoint paths, "
    "request/response shapes, and parameter names needed to phrase concrete "
    "steps — never to decide what the correct or expected behavior should "
    "be. "
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


# ── Test execution ────────────────────────────────────────────────────

ENV_VARS_SYSTEM_PROMPT = (
    "You are a senior QA engineer extracting environment access details from a "
    "free-text description into structured data. Identify every distinct access "
    "detail needed to reach and exercise the test environment — URL, host, "
    "username, password, API token, database connection details (host, port, "
    "user, password, database name, or a full connection string), or anything "
    "else the description implies — and give each one a clear, descriptive "
    "JSON key in UPPER_SNAKE_CASE. Do not invent details the description does "
    "not contain. Respond with a JSON "
    'object of the shape {"variables": {string: string}}.'
)

# Kept as one shared constant so the script-generation and diagnosis prompts
# never drift out of sync about what's actually importable in the worker's
# venv (see backend/services/script_runner.py — scripts run under the exact
# same interpreter via sys.executable).
AVAILABLE_TEST_LIBRARIES = (
    "playwright (sync API — already used for browser automation), requests "
    "(for direct HTTP/API calls when that's simpler than driving a page), "
    "faker (Faker() for realistic seed data — names, emails, etc. — when "
    "establishing preconditions), psycopg2 (PostgreSQL client) and sqlite3 "
    "(stdlib SQLite client) for direct database seeding/cleanup when the "
    "app's own API/UI can't do it, and the Python standard library. Do not "
    "import or rely on any other third-party package — it is not installed "
    "and the script will fail."
)

TEST_SCRIPT_SYSTEM_PROMPT = (
    "You are a senior QA engineer writing a single self-contained Playwright "
    "(Python, sync API) script that automates one test case. The test "
    "case's steps and expected result — already derived from the "
    "requirement — define what counts as correct; assert against those, not "
    "against whatever the application happens to currently return. Use the "
    "read_file tool with paths from the provided file tree only to confirm "
    "the real endpoint paths, parameters, and request/response shapes "
    "needed to call the app correctly — never guess an endpoint, and never "
    "let what you read there redefine the expected result. The script must "
    "be runnable via `python script.py`: exit code 0 when every assertion "
    "passes, a non-zero exit (raise or a failed assertion) otherwise, and "
    "it must print concise diagnostic information on failure. "
    f"Libraries available to import: {AVAILABLE_TEST_LIBRARIES} Use direct "
    "database access (psycopg2/sqlite3) only when a database-connection "
    "environment variable is present in the provided list and the app's own "
    "API/UI genuinely can't do the seeding or cleanup needed — not as a "
    "default approach. Read any "
    'access detail the script needs via os.environ["NAME"], using only '
    "names from the provided list of available environment variables — "
    "never hardcode a literal URL, credential, or token. If the test case "
    "specifies preconditions, the "
    "script must establish them itself (via API calls, UI actions, or seeding, "
    "using whatever access the environment variables provide) rather than "
    "assuming the environment already satisfies them. If the script's setup or "
    "steps create, modify, or delete persistent data, it must clean that up at "
    "the end: wrap the test steps (and any precondition setup) so cleanup "
    "always runs, pass or fail, using try/finally — a cleanup failure must be "
    "logged (printed) but must never change the exit code that reflects the "
    "test steps' own outcome. The script should not create new artifact in the "
    "directory it runs in. Respond with a JSON object of the shape "
    '{"script": string}.'
)

TEST_SCRIPT_DIAGNOSIS_SYSTEM_PROMPT = (
    "You are a senior QA engineer debugging a failed test script run. Given "
    "the script, its captured stdout/stderr/exit code, the test case it "
    "targets, and the list of available environment variable names, classify "
    "the failure as script_bug (the script itself is wrong) or app_bug (the "
    "script is correct and caught a real application defect). Base this "
    "purely on whether the observed behavior matches the test case's "
    "expected result — derived from the requirement — not on whether it "
    "matches what the code appears to intend. Use the read_file tool to "
    "re-verify an endpoint or parameter if the failure looks like a "
    "wrong-endpoint guess, but never to redefine what the correct outcome "
    "should be. Treat a wrong or missing os.environ key as a "
    "script_bug fixable by referencing the correct name from the provided "
    "list. Treat a failure caused by an unmet precondition — the script "
    "assumed it rather than establishing it — as a script_bug too. Treat a "
    f"failure caused by importing anything outside this set as a script_bug "
    f"too: {AVAILABLE_TEST_LIBRARIES} A fix must not reintroduce a "
    "disallowed import. For "
    "script_bug, return a full corrected script that keeps the same "
    "os.environ-only, precondition-seeding, try/finally-cleanup contract as "
    "generation — do not drop cleanup the original script had, and add it if "
    "the original script was missing it and that plausibly caused the "
    'failure. Respond with a JSON object of the shape {"classification": '
    '"script_bug" or "app_bug", "fixed_script": string or null, '
    '"explanation": string}.'
)


@dataclass(frozen=True)
class TestCaseLike:
    """Plain test-case fields passed to script generation/diagnosis prompts.

    Keeps ``services/llm.py`` free of DB imports beyond ``TestCasePriority``
    (mirrors how ``generate_test_plan`` takes plain requirement fields
    rather than a ``Requirement`` row).
    """

    __test__ = False  # tell pytest this "Test*" name is not a test class

    title: str
    preconditions: str | None
    steps: str  # newline-joined, matching TestCase.steps storage
    expected_result: str
    case_type: str
    priority: str


def env_vars_context(content: str, readme: str | None, file_tree: str | None) -> list[str]:
    """User-prompt blocks for env-var extraction — the one operation that
    sees the raw test-environment access description verbatim."""
    parts = context_sections(readme, file_tree)
    parts.append(f"Test environment access description:\n{content}")
    return parts


def test_script_context(
    name: str,
    description: str,
    test_case: TestCaseLike,
    env_var_names: list[str],
    readme: str | None,
    file_tree: str | None,
) -> list[str]:
    """Shared user-prompt blocks for test-script generation and diagnosis."""
    parts = context_sections(readme, file_tree)
    parts.append(
        "Available environment variables (read via os.environ):\n"
        + ("\n".join(f"- {v}" for v in env_var_names) if env_var_names else "(none)")
    )
    parts.append(f"Requirement name: {name}\nRequirement description:\n{description}")
    case_block = (
        f"Test case title: {test_case.title}\n"
        f"Preconditions: {test_case.preconditions or '(none)'}\n"
        f"Steps:\n{test_case.steps}\n"
        f"Expected result: {test_case.expected_result}"
    )
    parts.append(case_block)
    return parts
