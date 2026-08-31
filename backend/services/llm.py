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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, TypeVar

import httpx
import openai
from sqlmodel import SQLModel

from backend.config import (
    CICD_TOOL_ROUNDS,
    EXPLORATORY_CONTEXT_TOKEN_LIMIT,
    EXPLORATORY_MAX_CHARTERS,
    EXPLORATORY_MAX_FINDINGS,
    NONFUNCTIONAL_PLAN_TOOL_ROUNDS,
    NONFUNCTIONAL_TRIAGE_MAX_CHARS,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    OPENAI_TIMEOUT,
    TEST_EXECUTION_TOOL_ROUNDS,
)
from backend.models.database import SfdipotArea, TestCasePriority
from backend.services.llm_prompts import (
    BROWSER_TOOLS,
    CHARTER_SYSTEM_PROMPT,
    CHECK_SYSTEM_PROMPT,
    CICD_SYSTEM_PROMPT,
    ENV_VARS_SYSTEM_PROMPT,
    EXPLORATION_SUMMARY_SYSTEM_PROMPT,
    EXPLORATION_SYSTEM_PROMPT,
    FINDING_GROUPING_SYSTEM_PROMPT,
    HISTORY_COMPACTION_PROMPT,
    ITINERARY_WRAPUP_PROMPT,
    NONFUNCTIONAL_PLAN_SYSTEM_PROMPT,
    NONFUNCTIONAL_SUMMARY_SYSTEM_PROMPT,
    NONFUNCTIONAL_SYSTEM_PROMPT,
    NONFUNCTIONAL_TOOLS,
    NONFUNCTIONAL_TRIAGE_SYSTEM_PROMPT,
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
    FindingCandidate,
    KnownDefect,
    LoadProfileLike,
    TargetLike,
    TestCaseLike,
    ViolationLike,
    charter_context,
    cicd_context,
    context_sections,
    env_vars_context,
    exploration_context,
    exploration_summary_context,
    finding_grouping_context,
    nonfunctional_itinerary_context,
    nonfunctional_plan_context,
    nonfunctional_summary_context,
    nonfunctional_triage_context,
    requirements_section,
    test_plan_context,
    test_script_context,
)
from backend.utils.environment_utils import redact
from backend.utils.http_utils import SSL_CONTEXT

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
            http_client=httpx.Client(verify=SSL_CONTEXT, timeout=OPENAI_TIMEOUT),
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

    return _parse_json(content, model_cls)


def _require_followup(*, ok: bool, question: str | None, verdict: str) -> None:
    """A negative verdict must come with something to ask the user.

    Both clarification loops are driven entirely by the question: without
    one the row goes to `needs_clarification` / `needs_info` with nothing
    to show, and the user is stuck at a prompt that never appeared.  Four
    entry points state this same invariant, so it is checked in one place.
    """
    if not ok and not question:
        raise LLMError(f"LLM judged the {verdict} but gave no clarifying question.")


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
    _require_followup(
        ok=result.clear, question=result.clarifying_question, verdict="requirement unclear"
    )
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
    _require_followup(
        ok=result.clear, question=result.clarifying_question, verdict="requirement unclear"
    )
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
    _require_followup(
        ok=result.sufficient,
        question=result.clarifying_question,
        verdict="description insufficient",
    )
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
    _require_followup(
        ok=result.sufficient,
        question=result.clarifying_question,
        verdict="description insufficient",
    )
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


# Test planning takes no ``read_file`` executor, and takes no ``on_round``
# because there are no rounds to report. Both absences are structural on
# purpose. A plan defines what "correct" means for a requirement, so handing
# the model the implementation is precisely where that judgment drifts into
# describing what the code already does — the failure this codebase has hit
# before, and the reason the exploratory loop omits the tool too. Accepting
# ``read_file=None`` instead would leave re-enabling it one argument away.
#
# Nothing is lost: the interface details the old loop fetched are looked up
# again by ``generate_test_script`` at execution time, with a larger round
# budget and a fresher repo snapshot.


def generate_test_plan(
    name: str,
    description: str,
    sibling_names: list[str],
    test_env_content: str | None,
    readme: str | None,
    file_tree: str | None,
) -> TestPlanResult:
    """Generate a structured test plan for one requirement.

    One completion, grounded in the requirement, README, file tree, and test
    environment — never in the repository's code.
    """
    parts = test_plan_context(name, description, sibling_names, test_env_content, readme, file_tree)
    result = _complete(TEST_PLAN_SYSTEM_PROMPT, "\n\n".join(parts), TestPlanResult)
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
) -> TestPlanResult:
    """Revise a draft test plan per user feedback (same grounding + validation)."""
    parts = test_plan_context(name, description, sibling_names, test_env_content, readme, file_tree)
    parts.append(f"Current test plan (JSON):\n{current_plan_json}\n\nUser's feedback:\n{feedback}")
    result = _complete(TEST_PLAN_REVISE_SYSTEM_PROMPT, "\n\n".join(parts), TestPlanResult)
    return _validate_test_plan(result)


# ── Test execution ────────────────────────────────────────────────────


class EnvVarsResult(SQLModel):
    variables: dict[str, str]


class CicdFileItem(SQLModel):
    """One file the model creates.

    Create-only, so there is deliberately no ``action`` field: the field
    would have exactly one value, and a ``"modify"`` branch is what let a
    truncated rewrite of the team's own CI file read as a plausible diff.
    Modification goes through ``HostEdit``, where the splice is ours.
    """

    path: str
    content: str


class HostEdit(SQLModel):
    """One job (Actions) or stage (Jenkins) added to an existing CI file.

    ``job_body`` is the fragment and nothing more — the model never
    restates the host file, so truncating it is not expressible in this
    schema rather than merely discouraged.
    """

    path: str
    job_name: str
    job_body: str


class CicdIntegrationResult(SQLModel):
    """What the CI/CD generation call may return.

    Two absences are the design, and both are enforced by the schema rather
    than by a sentence in a prompt:

    * **no field can carry a test script.** Scripts are verified artefacts
      read from the database; the model integrates them and never rewrites
      one. This is the CI/CD analogue of keeping the test-planning call
      code-blind;
    * **no field can carry a whole host file.** The model authors a job or
      stage body and the splice is ours, so it cannot return a truncated
      version of a file the team wrote.
    """

    files: list[CicdFileItem] = []
    host_edit: HostEdit | None = None
    pr_title: str
    pr_body: str
    notes: str | None = None


class TestScriptResult(SQLModel):
    __test__ = False  # tell pytest this "Test*" name is not a test class

    script: str


class ScriptDiagnosisResult(SQLModel):
    __test__ = False  # tell pytest this "Test*" name is not a test class

    classification: Literal["script_bug", "app_bug"]
    fixed_script: str | None = None
    explanation: str
    # Bug report accompanying an app_bug verdict — the same shape an
    # exploratory session records. All optional, and deliberately not
    # validated below: the task normalizes them against the test case
    # instead. Raising here would route into _record_failure and retry the
    # entire TestExecution — re-running every remaining case — to punish a
    # formatting slip in a report whose substance is already in
    # `explanation`.
    finding_severity: str | None = None
    finding_title: str | None = None
    finding_steps_to_reproduce: str | None = None
    finding_expected: str | None = None
    finding_actual: str | None = None


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
    secrets: Mapping[str, str] | None = None,
) -> ScriptDiagnosisResult:
    """Classify a failed run as a script bug or app bug; fix script bugs in the same call.

    ``stdout``/``stderr`` are the one input to any LLM call in this
    application that can carry a **live environment value**: the script ran
    as a subprocess with the confirmed variables injected, so a traceback,
    an echoed response or an assertion message can reproduce one verbatim.
    Left alone, that value flows into ``fixed_script``, is cached on
    ``TestCase.script``, and is served by the script-download endpoint and
    committed by a CI/CD export.

    So it is rewritten here, at the point it would enter the conversation —
    the same treatment ``fill_secret`` gives the exploratory loop.  Values
    become ``$NAME``, which costs the diagnosis nothing: the model is
    handed ``env_var_names`` in the same prompt and is reading a script
    that fetches them from ``os.environ``.

    Only the prompt is affected.  ``TestCaseExecution.output`` is written
    from the raw result, so the authenticated user still sees the real
    output on the run page.
    """
    parts = test_script_context(name, description, test_case, env_var_names, readme, file_tree)
    secrets = secrets or {}
    parts.append(
        f"Script that was run:\n---\n{script}\n---\n\n"
        f"Exit code: {exit_code}\n"
        f"stdout:\n---\n{redact(stdout, secrets)}\n---\n\n"
        f"stderr:\n---\n{redact(stderr, secrets)}\n---"
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


# ── Nonfunctional testing ─────────────────────────────────────────────


class DomainProposalItem(SQLModel):
    domain: str
    applicable: bool = True
    rationale: str = ""


class LoadProfileItem(SQLModel):
    """One proposed load profile, in the only terms the model can be trusted with.

    Deliberately **not** a URL.  The model is never shown an environment
    variable's value, so an absolute URL is something it cannot construct —
    it would have to invent a host, and ``_validate_load_profiles`` would
    then refuse the result.  Asking instead for *which key pairs with which
    endpoint* makes the composed URL land on a confirmed origin **by
    construction**, so the origin check stops being a hope the model
    guessed the host right.

    The route composes ``base_url_env_var``'s real value with ``path`` and
    hands the UI a ``LoadProfileDraft`` carrying the absolute URL.
    """

    base_url_env_var: str
    path: str = "/"
    method: str = "GET"
    body: str | None = None
    concurrency: int = 1
    duration_seconds: int = 10
    total_request_cap: int = 100
    rationale: str = ""


class NonfunctionalPlanResult(SQLModel):
    domains: list[DomainProposalItem] = []
    base_url_env_vars: list[str] = []
    load_profiles: list[LoadProfileItem] = []


class TriagedFinding(SQLModel):
    """One violation written up.

    Note what is not here: **severity**, and any field naming a rule or a
    verdict. axe or the security table already graded this, and a schema
    with nowhere to put a second opinion is how that stays true — the
    prompt asking for prose is a request, but the schema is a wall.
    """

    id: str
    title: str
    steps_to_reproduce: str
    expected: str
    actual: str


class TriageResult(SQLModel):
    findings: list[TriagedFinding] = []


class NonfunctionalSummaryResult(SQLModel):
    summary: str


def generate_nonfunctional_plan(
    name: str,
    description: str,
    covered_cases: list[TestCaseLike],
    url_env_var_names: list[str],
    other_env_var_names: list[str],
    readme: str | None,
    file_tree: str | None,
    read_file: Callable[[str], str] | None,
) -> NonfunctionalPlanResult:
    """Propose domains, base URLs and load profiles for one requirement.

    Runs a bounded ``read_file`` loop (``NONFUNCTIONAL_PLAN_TOOL_ROUNDS``,
    deliberately shorter than the other two — this one runs synchronously
    inside a request).  The repo is what turns a load profile from a guess
    into a proposal: the file tree lists paths but not routes, so without
    reading source the model cannot say which endpoints exist or what
    methods they accept.

    The standing "a read_file tool becomes the model's oracle" hazard does
    not apply here, for the same reason it does not in
    ``generate_cicd_integration``: nothing this returns is a verdict.  It is
    a proposal a human edits and approves, and the nonfunctional oracle is
    inverted anyway — the tools grade, the model never does.

    Variable names arrive pre-split into those holding http(s) URLs and the
    rest, and **no value is ever sent**.  The model pairs a key with a path;
    the route resolves the key.

    ``on_round`` is a no-op: there is no heartbeat to keep in a synchronous
    request, and the caller is a route rather than a task row.
    """
    parts = nonfunctional_plan_context(
        name,
        description,
        covered_cases,
        url_env_var_names,
        other_env_var_names,
        readme,
        file_tree,
    )
    result = _complete_with_tools(
        NONFUNCTIONAL_PLAN_SYSTEM_PROMPT,
        "\n\n".join(parts),
        NonfunctionalPlanResult,
        read_file,
        lambda: None,
        NONFUNCTIONAL_PLAN_TOOL_ROUNDS,
    )
    if not result.base_url_env_vars:
        raise LLMError("LLM nominated no environment variable for the application URL.")
    if not any(domain.applicable for domain in result.domains):
        raise LLMError("LLM proposed no applicable domain for this requirement.")
    return result


def triage_nonfunctional_findings(
    violations: list[ViolationLike],
    max_chars: int = NONFUNCTIONAL_TRIAGE_MAX_CHARS,
    on_attempt: Callable[[], None] | None = None,
) -> dict[str, TriagedFinding]:
    """Write readable prose for violations the tools already found and graded.

    Returns a mapping **keyed by violation id**, never a list.  A batch that
    comes back one item short would otherwise re-label every finding after
    the gap — silently, and with plausible-looking prose, which is the worst
    shape a bug can take.  An id with no entry keeps its deterministic
    fallback text (``services/nonfunctional_checks``) instead of borrowing
    its neighbour's.

    Chunked at ``max_chars`` because the per-target cap alone allows
    ``MAX_TARGETS × AXE_MAX_CHARS`` in one request.  That would truncate or
    time out — and since the fallback text absorbs a triage failure, the
    symptom would not be an error but the LLM half of the feature quietly
    never running.

    Never raises: this call is optional by construction.  A chunk that fails
    costs its violations their prose, not the run.

    ``on_attempt`` fires once per chunk so a caller being watched for
    liveness can heartbeat.  It is not optional in practice: each chunk is
    an independent completion bounded by ``OPENAI_TIMEOUT``, so a run with
    several of them can out-wait ``HEARTBEAT_STALE_SECONDS`` and have the
    reconciler re-enqueue the whole browser walk as a crashed worker.  Same
    contract as ``summarize_exploration`` — heartbeating per unit of work
    makes the safety condition "one call shorter than the stale threshold"
    rather than arithmetic over how many there turned out to be.
    """
    written: dict[str, TriagedFinding] = {}
    for chunk in _chunk_violations(violations, max_chars):
        if on_attempt is not None:
            on_attempt()
        parts = nonfunctional_triage_context(chunk)
        try:
            result = _complete(NONFUNCTIONAL_TRIAGE_SYSTEM_PROMPT, "\n\n".join(parts), TriageResult)
        except LLMError as exc:
            logger.warning("Triage failed for a batch of %d violations: %s", len(chunk), exc)
            continue
        wanted = {violation.id for violation in chunk}
        for finding in result.findings:
            # An id we did not send is a hallucinated one — dropping it is
            # what keeps a stray entry from displacing a real violation.
            if finding.id in wanted:
                written[finding.id] = finding
    return written


def _chunk_violations(violations: list[ViolationLike], max_chars: int) -> list[list[ViolationLike]]:
    """Split violations into batches whose rendered context fits *max_chars*.

    A single violation over the limit still goes out alone: the alternative
    is dropping it, and one oversized item is a truncation risk while a
    dropped one is a lost finding.
    """
    batches: list[list[ViolationLike]] = []
    current: list[ViolationLike] = []
    size = 0
    for violation in violations:
        rendered = len(nonfunctional_triage_context([violation])[0])
        if current and size + rendered > max_chars:
            batches.append(current)
            current, size = [], 0
        current.append(violation)
        size += rendered
    if current:
        batches.append(current)
    return batches


def summarize_nonfunctional(
    name: str,
    description: str,
    targets: list[TargetLike],
    load_profiles: list[LoadProfileLike],
    on_attempt: Callable[[], None] | None = None,
) -> NonfunctionalSummaryResult:
    """Synthesize one run's targets and profiles into a narrative.

    Mirrors ``summarize_exploration`` — same retry count, same per-attempt
    heartbeat, same "callers treat exhaustion as non-fatal" contract.

    ``LoadProfileLike`` deliberately carries no request body: a body may hold
    a ``$NAME`` whose value is a credential, and the summary has nothing to
    say about it.
    """
    parts = nonfunctional_summary_context(name, description, targets, load_profiles)
    user_prompt = "\n\n".join(parts)

    for attempt in range(1, _SUMMARY_ATTEMPTS + 1):
        if on_attempt is not None:
            on_attempt()
        try:
            result = _complete(
                NONFUNCTIONAL_SUMMARY_SYSTEM_PROMPT, user_prompt, NonfunctionalSummaryResult
            )
            if not result.summary.strip():
                raise LLMError("LLM returned a blank nonfunctional summary.")
            return result
        except LLMError as exc:
            if attempt == _SUMMARY_ATTEMPTS:
                raise
            logger.warning(
                "Nonfunctional summary attempt %d of %d failed, retrying: %s",
                attempt,
                _SUMMARY_ATTEMPTS,
                exc,
            )

    # Unreachable: the loop either returns or re-raises on the last attempt.
    raise LLMError("Nonfunctional summary exhausted its attempts.")


# ── Finding grouping (issue-tracker de-duplication) ───────────────────


class FindingGroupItem(SQLModel):
    """One group of new findings, optionally matched to a known defect."""

    indices: list[int]
    existing_key: str | None = None


class FindingGroupingResult(SQLModel):
    groups: list[FindingGroupItem]


def group_findings(
    candidates: list[FindingCandidate],
    known: list[KnownDefect],
) -> FindingGroupingResult:
    """Decide which findings describe one defect, and which defect is already known.

    A single completion with no tools and no repository access.  Nothing
    is judged here except sameness: whether a finding is real was settled
    by the run that recorded it, and re-opening that question is exactly
    how a genuine bug gets talked out of existence on the way to the
    tracker.

    ``LLMError`` propagates — ``services/finding_dedup.py`` catches it and
    falls back to its deterministic prefilter, which is a worse grouping
    rather than no grouping.
    """
    parts = finding_grouping_context(candidates, known)
    return _complete(FINDING_GROUPING_SYSTEM_PROMPT, "\n\n".join(parts), FindingGroupingResult)


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


@dataclass(frozen=True)
class LoopProfile:
    """What differs between the two browser loops, and nothing else.

    Both loops drive the same browser through the same snapshot pruning,
    the same repeat detection, the same context compaction and the same
    forced wrap-up.  They differ in the seven things below — so the
    machinery has one implementation and a fix to it reaches both.

    ``tool_schema`` is the load-bearing one.  It is what goes on the wire
    as ``tools=``, and therefore what decides which tools the model is
    *offered*.  The executor dict is a separate object that merely
    dispatches a call already made: omitting an entry there makes a tool
    fail loudly, it does not make it unavailable.
    """

    system_prompt: str
    tool_schema: list[dict]
    terminal_tool: str
    wrapup_prompt: str
    nudge: str
    low_budget_tail: str
    normal_tail: str
    # A non-terminal tool that costs no action, or None. Exploratory
    # recording is free because findings are the session's deliverable and
    # charging them makes the model trade away its own output; a
    # nonfunctional run has no such tool, because its findings come from
    # the checks rather than from anything the model may call.
    free_tool: str | None = None


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

_WALK_OR_FINISH_NUDGE = (
    "That response did not call a tool, so nothing happened. Use the tool "
    "interface to act — do not describe a tool call in your message text. If "
    "you have reached everything this feature consists of, call "
    "finish_itinerary with your notes."
)

# Placeholder replacing a pruned snapshot tool result. Snapshots are the only
# large item in the conversation and a ref from many actions ago is stale
# anyway, so older ones are dropped verbatim rather than summarized by a
# second LLM call (see the brainstorm's Risk 1).
_PRUNED_SNAPSHOT = "[snapshot replaced to save context — take a fresh one if you need refs]"

# Consecutive identical tool calls before the model gets a nudge instead of
# another execution.
_REPEAT_NUDGE_THRESHOLD = 3

# Attempts for the two summary calls — the only retried calls in this module.
# They earn it by being the ones whose input cannot be cheaply reproduced: a
# charter or a plan regenerates from rows that are still in the database, but a
# summary reads session sheets from a browser run that took the sum of its
# charters to produce.
#
# This is purely a cost/latency choice (the summary-retry route has a user
# waiting on the response), *not* a timing constraint — both call sites
# heartbeat per attempt, so the reconciler cannot mistake a slow summary for a
# dead worker no matter how many attempts there are. Raising it is a one-line
# change; see ``summarize_exploration`` and ``_forced_wrap_up``.
_SUMMARY_ATTEMPTS = 2


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
    on_attempt: Callable[[], None] | None = None,
) -> ExplorationSummaryResult:
    """Synthesize a run's session sheets into a per-requirement summary.

    Retried up to ``_SUMMARY_ATTEMPTS`` times.  The retry is a plain re-send
    rather than a re-prompt with the error fed back: for a transport blip that
    is exactly right, and for malformed output it is a weaker but free second
    roll.  Feeding the error back would mean plumbing it through ``_complete``,
    which every other caller would pay for.

    ``on_attempt`` fires at the start of each attempt so a caller being watched
    for liveness can heartbeat.  The worker passes one because it calls this
    while the run is still ``running``; without it, retrying would widen the
    heartbeat gap and the reconciler could sweep a live summary as a dead
    worker.  Heartbeating per attempt makes the safety condition "one attempt
    shorter than the stale threshold" — the invariant ``config.py`` already
    promises between ``HEARTBEAT_STALE_SECONDS`` and ``OPENAI_TIMEOUT`` —
    instead of arithmetic over the attempt count.

    Callers treat exhaustion as non-fatal: the findings, not this paragraph,
    are the run's deliverable.
    """
    parts = exploration_summary_context(name, description, sessions)
    user_prompt = "\n\n".join(parts)

    for attempt in range(1, _SUMMARY_ATTEMPTS + 1):
        if on_attempt is not None:
            on_attempt()
        try:
            result = _complete(
                EXPLORATION_SUMMARY_SYSTEM_PROMPT, user_prompt, ExplorationSummaryResult
            )
            if not result.summary.strip():
                raise LLMError("LLM returned a blank exploration summary.")
            return result
        except LLMError as exc:
            if attempt == _SUMMARY_ATTEMPTS:
                raise
            logger.warning(
                "Exploration summary attempt %d of %d failed, retrying: %s",
                attempt,
                _SUMMARY_ATTEMPTS,
                exc,
            )

    # Unreachable: the loop either returns or re-raises on the last attempt.
    raise LLMError("Exploration summary exhausted its attempts.")


EXPLORATION_PROFILE = LoopProfile(
    system_prompt=EXPLORATION_SYSTEM_PROMPT,
    tool_schema=BROWSER_TOOLS,
    terminal_tool="finish_session",
    wrapup_prompt=SESSION_WRAPUP_PROMPT,
    nudge=_ACT_OR_FINISH_NUDGE,
    # Near the cap the advice changes: the forced wrap-up runs with
    # tool_choice="none", so record_finding is genuinely unreachable once the
    # budget is gone. A finding still unrecorded at that point can only ever
    # land in the notes, where nothing reads it as a finding.
    low_budget_tail=(
        "record anything you have found but not yet recorded — after "
        "your last action you can only write notes, not findings"
    ),
    normal_tail="call finish_session when the charter is explored",
    free_tool="record_finding",
)

NONFUNCTIONAL_PROFILE = LoopProfile(
    system_prompt=NONFUNCTIONAL_SYSTEM_PROMPT,
    tool_schema=NONFUNCTIONAL_TOOLS,
    terminal_tool="finish_itinerary",
    wrapup_prompt=ITINERARY_WRAPUP_PROMPT,
    nudge=_WALK_OR_FINISH_NUDGE,
    low_budget_tail="reach anything important you have not visited yet",
    normal_tail="call finish_itinerary once you have covered the feature",
    # No free tool: this loop has no recording tool to make free. Every
    # round is navigation, and navigation is what the budget is for.
    free_tool=None,
)


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
    on_round: Callable[[int], None],
    secrets: Mapping[str, str] | None = None,
    max_free_recordings: int = EXPLORATORY_MAX_FINDINGS,
    context_token_limit: int = EXPLORATORY_CONTEXT_TOKEN_LIMIT,
) -> ExplorationLoopResult:
    """Drive one charter's exploratory session.

    ``record_finding`` is free for the first ``max_free_recordings`` calls:
    findings are the session's whole deliverable, and charging them against
    the same budget as exploration makes the model trade away its own output
    under budget pressure.  It stops being free after that — ``actions_used <
    max_actions`` is the loop's only bound, so an unconditionally free
    non-terminal tool would remove the termination guarantee entirely (unlike
    ``finish_session``, which is free *and* terminal and therefore cannot
    loop).  Total rounds are thus capped at
    ``max_actions + max_free_recordings``.
    """
    parts = exploration_context(
        name, description, charter, sfdipot_areas, base_urls, env_var_names, readme, file_tree
    )
    user_prompt = (
        "\n\n".join(parts)
        + f"\n\nYou have {max_actions} actions for this session. Use them deliberately."
    )
    return _run_browser_loop(
        EXPLORATION_PROFILE,
        [
            {"role": "system", "content": EXPLORATION_PROFILE.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        tools=tools,
        max_actions=max_actions,
        snapshot_window=snapshot_window,
        on_round=on_round,
        secrets=secrets,
        max_free_calls=max_free_recordings,
        context_token_limit=context_token_limit,
    )


def run_nonfunctional_loop(
    name: str,
    description: str,
    covered_cases: list[TestCaseLike],
    base_urls: list[str],
    env_var_names: list[str],
    readme: str | None,
    file_tree: str | None,
    tools: dict[str, Callable[..., str]],
    max_actions: int,
    snapshot_window: int,
    on_round: Callable[[int], None],
    secrets: Mapping[str, str] | None = None,
    context_token_limit: int = EXPLORATORY_CONTEXT_TOKEN_LIMIT,
) -> ExplorationLoopResult:
    """Walk one requirement's feature so the catalogue runs everywhere.

    The same machinery as the exploratory loop and a deliberately different
    surface: ``NONFUNCTIONAL_TOOLS`` carries no ``record_finding``, so the
    model cannot report a violation even if the prompt somehow convinced it
    to try.  That is the whole inverted-oracle claim, and it lives here — in
    the request body — rather than in the executor dict, which only
    dispatches calls the model has already made.

    It also gets its own system prompt.  Reusing the exploratory one would
    spend three paragraphs teaching the model to call two tools it is not
    offered, which burns actions and teaches it the wrong loop.
    """
    parts = nonfunctional_itinerary_context(
        name, description, covered_cases, base_urls, env_var_names, readme, file_tree
    )
    user_prompt = (
        "\n\n".join(parts)
        + f"\n\nYou have {max_actions} actions. Spend them reaching screens, not studying them."
    )
    return _run_browser_loop(
        NONFUNCTIONAL_PROFILE,
        [
            {"role": "system", "content": NONFUNCTIONAL_PROFILE.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        tools=tools,
        max_actions=max_actions,
        snapshot_window=snapshot_window,
        on_round=on_round,
        secrets=secrets,
        max_free_calls=0,
        context_token_limit=context_token_limit,
    )


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


def _run_browser_loop(
    profile: LoopProfile,
    messages: list,
    tools: dict[str, Callable[..., str]],
    max_actions: int,
    snapshot_window: int,
    on_round: Callable[[int], None],
    secrets: Mapping[str, str] | None = None,
    max_free_calls: int = 0,
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

    ``on_round`` receives the actions consumed so far — unlike
    ``_complete_with_tools``'s bare heartbeat, this one doubles as the live
    progress feed, so the caller can persist a count that climbs during the
    session instead of appearing only once the loop returns.

    ``secrets`` is a redaction backstop for the action log: ``fill_secret``
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

    action_log: list[str] = []
    snapshot_indices: list[int] = []
    # Snapshot results are identified by tool_call_id rather than by position,
    # because compaction shifts every index after the span it removes.
    snapshot_call_ids: set[str] = set()
    last_signature: str | None = None
    repeat_count = 0
    actions_used = 0
    idle_rounds = 0
    free_calls_left = max_free_calls

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
                tools=profile.tool_schema,
            )
        except openai.OpenAIError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        on_round(actions_used)
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
                messages.append({"role": "user", "content": profile.nudge})
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

            if tool_name == profile.terminal_tool:
                notes = str(arguments.get("notes", "")).strip()
                action_log.append(f"{profile.terminal_tool}()")
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
            if tool_name == profile.free_tool and free_calls_left > 0:
                free_calls_left -= 1
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
                    f"{profile.terminal_tool} if there is nothing left to do."
                )
            else:
                executor = tools.get(tool_name)
                if executor is None:
                    result = f"ERROR: unknown tool {tool_name!r}."
                else:
                    result = executor(**arguments)

            action_log.append(
                f"{tool_name}({_format_args(arguments, secrets)}) -> {_summarize(result)}"
            )
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result})
            if tool_name == "snapshot":
                snapshot_call_ids.add(tool_call.id)
                snapshot_indices.append(len(messages) - 1)
                _prune_snapshots(messages, snapshot_indices, snapshot_window)

        remaining = max_actions - actions_used
        # Near the cap the advice changes — see each profile's two tails.
        # The forced wrap-up runs with tool_choice="none", so whatever the
        # loop still wanted done has to happen before the budget is gone.
        tail = profile.low_budget_tail if remaining <= _LOW_BUDGET_ACTIONS else profile.normal_tail
        messages[-1]["content"] += f"\n[{remaining} of {max_actions} actions remaining — {tail}]"

        # Report the round's actions as soon as they are spent. The heartbeat
        # above fires before the tools run, so without this the persisted count
        # would trail a whole LLM round behind what the session has done.
        on_round(actions_used)

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
                    client,
                    profile,
                    messages,
                    on_round,
                    STOP_CONTEXT_LIMIT,
                    actions_used,
                    action_log,
                )
            on_round(actions_used)
            if not compacted:
                # Nothing left to compact while still over the limit: the
                # floor exceeds it, so retrying next round would only thrash.
                logger.warning("Exploration context over limit with nothing left to compact")
                return _forced_wrap_up(
                    client,
                    profile,
                    messages,
                    on_round,
                    STOP_CONTEXT_LIMIT,
                    actions_used,
                    action_log,
                )
            snapshot_indices = [
                index
                for index, item in enumerate(messages)
                if _is_tool_result(item) and item.get("tool_call_id") in snapshot_call_ids
            ]

    return _forced_wrap_up(
        client, profile, messages, on_round, STOP_ACTION_CAP, actions_used, action_log
    )


def _forced_wrap_up(
    client,
    profile: LoopProfile,
    messages: list,
    on_round: Callable[[int], None],
    stop_reason: str,
    actions_used: int,
    action_log: list[str],
) -> ExplorationLoopResult:
    """Ask for the session notes and stop, however the session ran out.

    Shared by the action cap and both context-limit exits.  Keeps JSON mode —
    unlike the acting rounds, here the JSON object genuinely is the wanted
    output, and ``SESSION_WRAPUP_PROMPT`` asks for it.

    **Never raises**, because this is the most expensive call in the system to
    lose: ``action_log`` exists only in this call stack, so an exception would
    have ``_run_one_session`` mark the session ``error`` and discard the entire
    record of a charter that already spent its whole action budget driving a
    real browser.  Exploration is over by the time the wrap-up is asked for, so
    a failure here costs the notes, never the session.

    Three degrees, in order: retry up to ``_SUMMARY_ATTEMPTS`` times; fall back
    to the raw message text when the JSON will not parse (the same salvage the
    idle-rounds exit above already does — unparseable notes are still notes);
    and if the request itself never succeeds, say so *in* the notes.  A reader
    looking at the session sheet sees why it has no wrap-up, which is what the
    ``error`` status would otherwise have told them, minus the collateral.

    ``on_round`` doubles as the per-attempt heartbeat (it is already the
    caller's liveness signal), so retrying here cannot widen the heartbeat gap
    enough for the reconciler to sweep a live session.

    The wrap-up prompt is appended once, outside the retry loop: appending it
    per attempt would send the model two wrap-up instructions.
    """
    messages.append({"role": "user", "content": profile.wrapup_prompt})

    notes: str | None = None
    for attempt in range(1, _SUMMARY_ATTEMPTS + 1):
        on_round(actions_used)
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=profile.tool_schema,
                tool_choice="none",
                response_format={"type": "json_object"},
            )
        except openai.OpenAIError as exc:
            if attempt == _SUMMARY_ATTEMPTS:
                # Report the failure as the notes rather than raising. The
                # action log exists only in this frame, so raising would take
                # the whole session record down with it — and exploration had
                # already finished by the time the wrap-up was asked for, so
                # there is nothing dishonest about ending the session here.
                logger.warning("Session wrap-up request failed on every attempt: %s", exc)
                notes = f"(session wrap-up unavailable — the LLM request failed: {exc})"
                break
            logger.warning(
                "Session wrap-up attempt %d of %d failed, retrying: %s",
                attempt,
                _SUMMARY_ATTEMPTS,
                exc,
            )
            continue

        content = response.choices[0].message.content
        try:
            notes = _parse_json(content, SessionWrapUpResult).notes.strip()
            break
        except LLMError as exc:
            if attempt == _SUMMARY_ATTEMPTS:
                # Keep what the model actually said: unparseable notes are
                # still the session record, and losing them costs the charter.
                logger.warning("Session wrap-up unparseable, keeping raw text: %s", exc)
                notes = (content or "").strip()
                break
            logger.warning(
                "Session wrap-up attempt %d of %d returned unparseable output, retrying: %s",
                attempt,
                _SUMMARY_ATTEMPTS,
                exc,
            )

    return ExplorationLoopResult(
        notes=notes or "(session ended without notes)",
        stop_reason=stop_reason,
        actions_used=actions_used,
        action_log=action_log,
    )


def _format_args(arguments: dict, secrets: Mapping[str, str] | None = None) -> str:
    """Render tool arguments for the action log.

    ``fill_secret`` is logged by variable name only — its value is resolved
    inside the executor and never reaches this module, which is what keeps
    the stored log credential-free by construction.

    ``secrets`` is the backstop for the one path that bypasses it: a model
    that ignores the instruction and types a credential through plain
    ``fill``.  Matching is **exact**, not substring — the model never sees
    environment values, so a leak means it reproduced one verbatim, whereas
    substring matching would mangle ordinary log lines that happen to
    contain a short value.

    The replacement is the variable's own name, matching every other
    redaction in the application: ``value=$QA_PASSWORD`` tells a reader
    which credential was typed, where a blanking placeholder tells them
    only that something was.
    """
    by_value = {value: name for name, value in sorted((secrets or {}).items())}

    def render(value) -> str:
        if isinstance(value, str) and value in by_value:
            return f"${by_value[value]}"
        return repr(value)

    return ", ".join(f"{key}={render(value)}" for key, value in arguments.items())


def _summarize(result: str, limit: int = 200) -> str:
    """Condense a tool result for the action log (snapshots are huge)."""
    collapsed = " ".join(result.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"


def _validate_cicd_result(result: CicdIntegrationResult) -> CicdIntegrationResult:
    """Reject output that cannot become a pull request.

    Only what the *schema* cannot express: emptiness, and a host edit
    missing the pieces the splice needs.  Everything about paths,
    structure, references and secrets belongs to ``cicd_export.validate``,
    which runs against the repository's own files and is the one place
    model output becomes a filesystem effect.
    """
    if not result.pr_title.strip():
        raise LLMError("LLM returned a pull request with no title.")
    if not result.files and result.host_edit is None:
        raise LLMError("LLM returned no CI files and no host edit — nothing to commit.")
    for item in result.files:
        if not item.path.strip():
            raise LLMError("LLM returned a CI file with no path.")
        if not item.content.strip():
            raise LLMError(f"LLM returned an empty CI file: {item.path}")
    if result.host_edit is not None:
        edit = result.host_edit
        if not edit.path.strip() or not edit.job_name.strip() or not edit.job_body.strip():
            raise LLMError("LLM returned an incomplete host edit.")
    return result


def generate_cicd_integration(
    provider: str,
    readme: str | None,
    file_tree: str | None,
    ci_facts: str,
    ci_environment_hint: str | None,
    variable_names: list[str],
    secret_names: list[str],
    script_paths: list[str],
    deterministic_block: str,
    host_candidates: list[str],
    read_file: Callable[[str], str] | None,
    on_round: Callable[[], None],
) -> CicdIntegrationResult:
    """Author the CI configuration that runs an already-verified suite.

    Runs the same bounded ``read_file`` loop script generation uses.  The
    standing "a read_file tool becomes the model's oracle" hazard does not
    apply here: that hazard is about treating implementation code as the
    definition of *correct* when judging a product.  This call judges
    nothing — reading the repository **is** the task, and the repository is
    the only possible source of truth for "how does CI here install
    Playwright".
    """
    parts = cicd_context(
        provider,
        readme,
        file_tree,
        ci_facts,
        ci_environment_hint,
        variable_names,
        secret_names,
        script_paths,
        deterministic_block,
        host_candidates,
    )
    result = _complete_with_tools(
        CICD_SYSTEM_PROMPT,
        "\n\n".join(parts),
        CicdIntegrationResult,
        read_file,
        on_round,
        CICD_TOOL_ROUNDS,
    )
    return _validate_cicd_result(result)
