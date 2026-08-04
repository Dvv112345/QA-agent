from datetime import datetime

from sqlmodel import SQLModel

from backend.models.database import TestCasePriority

# ── Health ────────────────────────────────────────────────────────────


class HealthResponse(SQLModel):
    status: str
    storage: str = "unknown"
    redis: str = "unknown"


# ── Auth (preserved) ──────────────────────────────────────────────────


class PasswordVerifyRequest(SQLModel):
    password: str


class AuthCheckResponse(SQLModel):
    valid: bool


# ── Repo ──────────────────────────────────────────────────────────────


class RepoResponse(SQLModel):
    id: int
    github_link: str
    name: str
    description: str | None = None
    active: bool
    created_at: datetime


class ReadmeStatusResponse(SQLModel):
    has_readme: bool


# ── Sprint ────────────────────────────────────────────────────────────


class SprintResponse(SQLModel):
    id: int
    name: str
    repo_id: int
    active: bool
    directory: str
    created_at: datetime
    repo: RepoResponse | None = None
    requirements_complete: bool = False
    has_test_environment_submission: bool = False
    environment_confirmed: bool = False
    has_test_plans: bool = False
    # A confirmed requirement with no plan — the state a requirement edit
    # leaves behind, and invisible in the plan list itself.
    test_plans_missing: bool = False
    test_plans_complete: bool = False
    has_test_runs: bool = False
    has_exploratory_runs: bool = False


class SprintUpdateRequest(SQLModel):
    active: bool


# ── Requirement ───────────────────────────────────────────────────────


class RequirementCreateRequest(SQLModel):
    name: str
    description: str


class RequirementAnswerRequest(SQLModel):
    answer: str


class RequirementEditRequest(SQLModel):
    description: str


class RequirementResponse(SQLModel):
    id: int
    sprint_id: int
    name: str
    description: str
    original_description: str
    from_prd: bool
    status: str
    clarifying_question: str | None = None
    revision_count: int
    clarification_cap_reached: bool
    error: str | None = None
    created_at: datetime
    updated_at: datetime


# ── Test environment access ───────────────────────────────────────────


class TestEnvironmentSubmitRequest(SQLModel):
    content: str


class TestEnvironmentAnswerRequest(SQLModel):
    answer: str


class TestEnvironmentResponse(SQLModel):
    id: int
    sprint_id: int
    content: str
    original_content: str
    status: str
    clarifying_question: str | None = None
    revision_count: int
    clarification_cap_reached: bool
    requirements_stale: bool
    env_vars: dict[str, str] | None = None
    created_at: datetime
    updated_at: datetime


class TestEnvironmentVarsEditRequest(SQLModel):
    variables: dict[str, str]


# ── Test plan ─────────────────────────────────────────────────────────


class TestCaseResponse(SQLModel):
    id: int
    position: int
    title: str
    preconditions: str | None = None
    steps: str
    expected_result: str
    case_type: str
    priority: TestCasePriority


class TestPlanResponse(SQLModel):
    id: int
    requirement_id: int
    requirement_name: str
    requirement_description: str
    status: str
    complexity: str | None = None
    summary: str | None = None
    revision_count: int
    feedback_cap_reached: bool
    error: str | None = None
    cases: list[TestCaseResponse] = []
    created_at: datetime
    updated_at: datetime


class TestPlanFeedbackRequest(SQLModel):
    feedback: str


class TestCaseInput(SQLModel):
    title: str
    preconditions: str | None = None
    steps: str
    expected_result: str
    case_type: str
    priority: TestCasePriority


class TestPlanEditRequest(SQLModel):
    complexity: str
    summary: str
    cases: list[TestCaseInput]


# ── Findings (shared by scripted and exploratory testing) ─────────────


class FindingBase(SQLModel):
    """The seven fields every finding carries, whoever found it.

    Shared so a reader — and the one frontend card that renders both — sees
    the same shape whether an exploratory session recorded it live or a
    scripted run derived it from a failed test case.
    """

    finding_type: str
    severity: str
    title: str
    steps_to_reproduce: str
    expected: str
    actual: str
    # Where it was observed. None on findings recorded before capture
    # existed — normal for old rows, not an error.
    environment: str | None = None


class TestCaseFindingResponse(FindingBase):
    """A scripted run's finding — the shared fields and nothing else.

    No screenshot: a subprocess has no page to photograph. That absence is
    already the normal case for exploratory findings under
    ``STORE_OFFLINE=false``, so the card handles it without special-casing.
    """

    __test__ = False  # tell pytest this "Test*" name is not a test class


# ── Test execution ────────────────────────────────────────────────────


class TestCaseExecutionResponse(SQLModel):
    id: int
    test_case: TestCaseResponse
    status: str
    attempts: int
    output: str | None = None
    error: str | None = None
    # Populated from TestCaseExecution.finding — None unless the case ended
    # in a terminal failure that recorded one. Raw output above stays put:
    # it is the debugging surface, this is the report.
    finding: TestCaseFindingResponse | None = None
    updated_at: datetime


class TestExecutionResponse(SQLModel):
    id: int
    requirement_id: int
    requirement_name: str
    status: str
    error: str | None = None
    # Upstream artifacts that have changed since the run executed, from
    # {"requirement", "test_plan", "test_environment"}. Empty means current;
    # the frontend derives its boolean from this rather than a second field.
    outdated_reasons: list[str] = []
    # Selects the wording for the "requirement" reason ("deleted" rather
    # than "changed"). Never a correctness branch on its own.
    requirement_deleted: bool = False
    cases: list[TestCaseExecutionResponse] = []
    created_at: datetime
    updated_at: datetime


class TestRunResponse(SQLModel):
    id: int
    sprint_id: int
    created_at: datetime
    status: str
    # Upstream artifacts that have changed since the run executed, from
    # {"requirement", "test_plan", "test_environment"}. Empty means current;
    # the frontend derives its boolean from this rather than a second field.
    outdated_reasons: list[str] = []
    # Selects the wording for the "requirement" reason ("deleted" rather
    # than "changed"). Never a correctness branch on its own.
    requirement_deleted: bool = False
    requirement_names: list[str]
    total_cases: int
    passed_cases: int
    failed_cases: int
    error_cases: int


class TestRunDetailResponse(SQLModel):
    id: int
    sprint_id: int
    created_at: datetime
    status: str
    # Upstream artifacts that have changed since the run executed, from
    # {"requirement", "test_plan", "test_environment"}. Empty means current;
    # the frontend derives its boolean from this rather than a second field.
    outdated_reasons: list[str] = []
    # Selects the wording for the "requirement" reason ("deleted" rather
    # than "changed"). Never a correctness branch on its own.
    requirement_deleted: bool = False
    executions: list[TestExecutionResponse] = []


class TestRunCreateRequest(SQLModel):
    requirement_ids: list[int]


# ── Exploratory testing ───────────────────────────────────────────────


class ExploratoryFindingResponse(FindingBase):
    id: int
    position: int
    # Whether a screenshot exists — the path itself is never exposed, and
    # None is normal when STORE_OFFLINE is false, not an error.
    has_screenshot: bool = False
    created_at: datetime


class ExploratorySessionSummaryResponse(SQLModel):
    """List shape — omits ``action_log``, which only the detail view needs."""

    id: int
    position: int
    charter: str
    sfdipot_areas: list[str] = []
    status: str
    actions_used: int
    stop_reason: str | None = None
    error: str | None = None
    finding_count: int = 0
    updated_at: datetime


class ExploratorySessionResponse(SQLModel):
    id: int
    exploratory_run_id: int
    position: int
    charter: str
    sfdipot_areas: list[str] = []
    status: str
    actions_used: int
    session_notes: str | None = None
    action_log: str | None = None
    stop_reason: str | None = None
    error: str | None = None
    findings: list[ExploratoryFindingResponse] = []
    updated_at: datetime


class ExploratoryRunResponse(SQLModel):
    """List-page shape — aggregates computed at response time, never stored."""

    id: int
    sprint_id: int
    requirement_id: int
    requirement_name: str
    status: str
    summary: str | None = None
    error: str | None = None
    # Upstream artifacts that have changed since the run executed, from
    # {"requirement", "test_plan", "test_environment"}. Empty means current;
    # the frontend derives its boolean from this rather than a second field.
    outdated_reasons: list[str] = []
    # Selects the wording for the "requirement" reason ("deleted" rather
    # than "changed"). Never a correctness branch on its own.
    requirement_deleted: bool = False
    session_count: int = 0
    bug_count: int = 0
    issue_count: int = 0
    high_severity_count: int = 0
    created_at: datetime
    updated_at: datetime


class ExploratoryRunDetailResponse(SQLModel):
    id: int
    sprint_id: int
    requirement_id: int
    requirement_name: str
    status: str
    summary: str | None = None
    error: str | None = None
    # Upstream artifacts that have changed since the run executed, from
    # {"requirement", "test_plan", "test_environment"}. Empty means current;
    # the frontend derives its boolean from this rather than a second field.
    outdated_reasons: list[str] = []
    # Selects the wording for the "requirement" reason ("deleted" rather
    # than "changed"). Never a correctness branch on its own.
    requirement_deleted: bool = False
    base_url_env_vars: list[str] = []
    sessions: list[ExploratorySessionSummaryResponse] = []
    bug_count: int = 0
    issue_count: int = 0
    high_severity_count: int = 0
    created_at: datetime
    updated_at: datetime


class CharterDraft(SQLModel):
    """One proposed charter — request and response shape both."""

    charter: str
    sfdipot_areas: list[str] = []


class ExploratoryCharterDraftResponse(SQLModel):
    """Drafted charters plus everything the review screen needs.

    ``projected_minutes`` is heuristic arithmetic over the charter count
    (Convention #10 — computed server-side so the frontend never reassembles
    it from config literals). It is deliberately not something the LLM was
    asked to estimate.
    """

    requirement_id: int
    requirement_name: str
    charters: list[CharterDraft] = []
    base_url_env_vars: list[str] = []
    charter_count: int = 0
    projected_minutes: int = 0


class ExploratoryCharterGenerateRequest(SQLModel):
    requirement_id: int


class ExploratoryRunCreateRequest(SQLModel):
    requirement_id: int
    charters: list[CharterDraft]
    base_url_env_vars: list[str]
