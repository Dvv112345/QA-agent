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
    # Whether a token is stored, never the token (Convention #10): the
    # issue-tracker form needs to know if the repo can supply a credential.
    has_access_token: bool = False


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


# ── Issue tracker ─────────────────────────────────────────────────────


class IssueTrackerConfigRequest(SQLModel):
    """Create-or-edit payload for a sprint's tracker connection.

    Every field but ``provider`` and ``target`` is optional here rather
    than in the schema: which ones are actually required depends on the
    provider, and on whether this is an edit that keeps the stored token.
    The route validates that combination and 422s, so the error names the
    missing field instead of reading as a malformed request.
    """

    provider: str
    target: str  # Jira project key | "owner/repo"
    base_url: str | None = None  # Jira site root
    account_email: str | None = None  # Jira Basic-auth user
    # Blank or absent means "keep the stored token" on a same-provider
    # edit; required when the provider changes, since a Jira API token is
    # meaningless to GitHub.
    api_token: str | None = None
    issue_type: str | None = None  # Jira issue type name
    # GitHub only: file into the sprint's own registered repository. The
    # route derives ``owner/repo`` from ``Repo.github_link`` (so ``target``
    # may be blank) and, when no token is typed, uses the repo's stored one.
    use_sprint_repo: bool = False


class TrackerIssueGroup(SQLModel):
    """One filed ticket and how many of a run's findings it stands for.

    Exposed alongside the counts because grouping is the whole point: six
    findings can be two tickets, and a reader should not have to open
    every card to work out which four became QA-142.
    """

    issue_key: str
    issue_url: str
    finding_count: int


class ExportRollup(SQLModel):
    """A run's export state, computed at response time and never stored.

    ``export_error_count`` is a **subset** of ``unexported_finding_count``
    rather than disjoint from it: one condition decides whether the page
    offers the button, the other words it.
    """

    # The run's own toggle — the one *stored* field here, and the only
    # thing that distinguishes "was set to file and has not yet" from
    # "was never set to file". The button files either way, so this only
    # ever changes the wording.
    export_findings: bool = False
    exported_finding_count: int = 0
    exported_issue_count: int = 0
    export_error_count: int = 0
    unexported_finding_count: int = 0
    export_groups: list[TrackerIssueGroup] = []


class IssueTrackerConfigResponse(SQLModel):
    """The connection as the UI sees it — never the token."""

    id: int
    sprint_id: int
    provider: str
    target: str
    # "Jira · QA" — computed server-side so the panel never reassembles a
    # provider label from a raw enum value (Convention #10 spirit).
    target_label: str
    base_url: str | None = None
    account_email: str | None = None
    issue_type: str | None = None
    verified_at: datetime
    created_at: datetime
    updated_at: datetime


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
    # ── issue-tracker receipt ──
    # All null on a finding that was never filed: the run's toggle was off,
    # the run did not reach the completion path, or filing has not run yet.
    # `tracker_target` is deliberately absent — it exists to scope
    # de-duplication in the database, and `tracker_issue_url` is already
    # absolute, so the card needs nothing further to link the ticket.
    tracker_issue_key: str | None = None
    tracker_issue_url: str | None = None
    tracker_error: str | None = None
    # True when this finding was grouped into another finding's ticket
    # rather than getting one of its own.
    tracker_is_duplicate: bool = False


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


class TestRunResponse(ExportRollup):
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


class TestRunDetailResponse(ExportRollup):
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
    # Whether this run's bug findings are filed to the sprint's issue
    # tracker when it completes. Decided at run start and never after:
    # the run is what carries the decision, so a tracker connected (or
    # disconnected) later cannot retroactively change what a finished run
    # was supposed to do.
    export_findings: bool = False


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


class ExploratoryRunResponse(ExportRollup):
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


class ExploratoryRunDetailResponse(ExportRollup):
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
    # See TestRunCreateRequest.export_findings.
    export_findings: bool = False


# ── QA metrics ────────────────────────────────────────────────────────


class RequirementMetrics(SQLModel):
    """One requirement's row in the sprint breakdown.

    Present for **every** requirement a counted run covered, including
    those with no findings at all: a tested-and-clean requirement is a
    result worth showing, and a table listing only requirements with bugs
    reads as the whole picture when it is half of it.
    """

    requirement_id: int
    requirement_name: str
    # Archived requirements stay in the breakdown, flagged. Their bugs are
    # in the headline, so hiding the row would make the numbers not add up
    # — the failure mode ``FindingType.normalize``'s docstring names.
    requirement_deleted: bool = False
    bug_count: int
    issue_count: int
    # Distinct, matching the sprint-level headline (see below).
    distinct_test_cases_run: int
    exploratory_sessions: int


class SprintMetricsResponse(SQLModel):
    """How QA went for one sprint, computed at response time.

    Never stored — the treatment ``export_rollup`` already established for
    exactly this problem, one level up.  The definitions live in Python
    (Convention #10): a frontend dividing ``bug_count / requirement_count``
    itself would be one more place they can drift, and could not do the
    ticket collapse at all.
    """

    sprint_id: int
    # ── scripted: two levels, deliberately not reconciled ──
    # ``distinct_test_cases_run`` is the density denominator; the execution
    # counts describe how much testing was done. A case run three times
    # adds 1 to the first and 3 across the second. Keeping them separate is
    # what removes any need for a "what status does that case have?"
    # tiebreak — each level's numbers add up within itself.
    distinct_test_cases_run: int
    case_executions: int
    executions_passed: int
    executions_failed: int
    executions_errored: int
    # ── exploratory ──
    # Never summed with the scripted counts: a 25-action browser session
    # and a 3-step script are not the same unit.
    exploratory_sessions: int
    requirements_explored: int
    # ── defects (distinct, after collapse) ──
    bug_count: int
    issue_count: int
    # A group's severity is the **highest** among its members, mirroring
    # how ``finding_dedup._elect`` picks a group's representative (highest
    # severity, then lowest position). Taking the first member's would let
    # a high-severity defect hide behind a medium duplicate.
    high_severity_bug_count: int
    # ── density ──
    # ``requirements_covered`` = distinct requirements (archived included)
    # touched by the *counted* runs, either mode. It is the denominator of
    # ``bugs_per_requirement``: a sprint that tested 1 of 5 features must
    # not divide its real bugs across 4 nobody touched, which would report
    # a fifth of the true density and improve every time an untested
    # requirement is added. ``requirements_total`` sits beside it so
    # coverage stays legible rather than being baked invisibly into the
    # ratio.
    requirements_covered: int
    requirements_total: int
    # None when the denominator is zero, so the UI renders "—" and there is
    # no divide guard in TSX.
    bugs_per_requirement: float | None = None
    bugs_per_test_case: float | None = None
    # ── breakdown ──
    # Ordered by bug count descending, then by name: the reader's question
    # is "which feature is worst", and id order buries it.
    per_requirement: list[RequirementMetrics] = []
    # ── exclusions ──
    # Only completed runs feed the numbers, mirroring the export rule: a
    # run that finished reports; anything else waits for a human. Counted
    # and named rather than silently dropped, so a short number is never
    # silently short.
    excluded_runs_running: int = 0
    excluded_runs_failed: int = 0
