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
    requirements_locked: bool = False
    has_test_plans: bool = False
    test_plans_complete: bool = False
    has_test_runs: bool = False


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


# ── Test execution ────────────────────────────────────────────────────


class TestCaseExecutionResponse(SQLModel):
    id: int
    test_case: TestCaseResponse
    status: str
    attempts: int
    output: str | None = None
    error: str | None = None
    updated_at: datetime


class TestExecutionResponse(SQLModel):
    id: int
    requirement_id: int
    requirement_name: str
    status: str
    error: str | None = None
    cases: list[TestCaseExecutionResponse] = []
    created_at: datetime
    updated_at: datetime


class TestRunResponse(SQLModel):
    id: int
    sprint_id: int
    created_at: datetime
    status: str
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
    executions: list[TestExecutionResponse] = []


class TestRunCreateRequest(SQLModel):
    requirement_ids: list[int]
