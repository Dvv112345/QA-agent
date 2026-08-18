export interface PasswordVerifyRequest {
  password: string
}

export interface AuthCheckResponse {
  valid: boolean
}

export interface RepoResponse {
  id: number
  github_link: string
  name: string
  description: string | null
  active: boolean
  created_at: string
  /** Whether a token is stored — never the token itself. */
  has_access_token: boolean
}

export interface SprintResponse {
  id: number
  name: string
  repo_id: number
  active: boolean
  directory: string
  created_at: string
  repo: RepoResponse | null
  requirements_complete: boolean
  has_test_environment_submission: boolean
  environment_confirmed: boolean
  has_test_plans: boolean
  /** A confirmed requirement has no plan — what a requirement edit leaves behind. */
  test_plans_missing: boolean
  test_plans_complete: boolean
  has_test_runs: boolean
  has_exploratory_runs: boolean
}

export interface ReadmeStatusResponse {
  has_readme: boolean
}

export type RequirementStatus =
  'pending' | 'analyzing' | 'needs_clarification' | 'ready' | 'confirmed' | 'failed'

export interface RequirementInput {
  name: string
  description: string
}

export interface RequirementResponse {
  id: number
  sprint_id: number
  name: string
  description: string
  original_description: string
  from_prd: boolean
  status: RequirementStatus
  clarifying_question: string | null
  revision_count: number
  clarification_cap_reached: boolean
  error: string | null
  created_at: string
  updated_at: string
}

export type TestEnvironmentStatus = 'needs_info' | 'ready' | 'confirmed'

export type TestPlanStatus = 'pending' | 'generating' | 'draft' | 'approved' | 'failed'

export type TestCasePriority = 'high' | 'medium' | 'low'

export interface TestCaseResponse {
  id: number
  position: number
  title: string
  preconditions: string | null
  steps: string
  expected_result: string
  case_type: string
  priority: TestCasePriority
}

export interface TestPlanResponse {
  id: number
  requirement_id: number
  requirement_name: string
  requirement_description: string
  status: TestPlanStatus
  complexity: string | null
  summary: string | null
  revision_count: number
  feedback_cap_reached: boolean
  error: string | null
  cases: TestCaseResponse[]
  created_at: string
  updated_at: string
}

export interface TestCaseInput {
  title: string
  preconditions: string | null
  steps: string
  expected_result: string
  case_type: string
  priority: TestCasePriority
}

export interface TestPlanEditRequest {
  complexity: string
  summary: string
  cases: TestCaseInput[]
}

export interface TestEnvironmentResponse {
  id: number
  sprint_id: number
  content: string
  original_content: string
  status: TestEnvironmentStatus
  clarifying_question: string | null
  revision_count: number
  clarification_cap_reached: boolean
  requirements_stale: boolean
  env_vars: Record<string, string> | null
  created_at: string
  updated_at: string
}

export interface TestEnvironmentVarsEditRequest {
  variables: Record<string, string>
}

// ── Findings (shared by scripted and exploratory testing) ────────────

export type FindingType = 'bug' | 'issue'

export type FindingSeverity = 'high' | 'medium' | 'low'

/**
 * The fields every finding carries, whoever found it. FindingCard renders
 * this shape, so an exploratory session's live capture and a scripted run's
 * failed case read identically.
 */
export interface Finding {
  finding_type: FindingType
  severity: FindingSeverity
  title: string
  steps_to_reproduce: string
  expected: string
  actual: string
  /** Where it was observed. Null on findings recorded before capture existed. */
  environment: string | null
  /**
   * Issue-tracker receipt. All null/false on a finding that was never
   * filed: the run's toggle was off, the run did not reach the completion
   * path, or filing has not run yet. The URL is absolute, so the card
   * needs nothing else to link the ticket.
   */
  tracker_issue_key: string | null
  tracker_issue_url: string | null
  tracker_error: string | null
  /** Grouped into another finding's ticket rather than getting its own. */
  tracker_is_duplicate: boolean
}

// ── Issue tracker ───────────────────────────────────────────────────

export type IssueTrackerProvider = 'jira' | 'github'

export interface IssueTrackerConfig {
  id: number
  sprint_id: number
  provider: IssueTrackerProvider
  /** Jira project key, or "owner/repo". */
  target: string
  /** "Jira · QA" — composed server-side, never reassembled here. */
  target_label: string
  base_url: string | null
  account_email: string | null
  issue_type: string | null
  verified_at: string
  created_at: string
  updated_at: string
}

/** The token is write-only: blank means "keep the stored one". */
export interface IssueTrackerConfigInput {
  provider: IssueTrackerProvider
  target: string
  base_url?: string | null
  account_email?: string | null
  api_token?: string | null
  issue_type?: string | null
  /**
   * GitHub only: file into the sprint's own repository. The backend derives
   * `owner/repo` (so `target` may be blank) and falls back to the repo's
   * stored access token when none is typed.
   */
  use_sprint_repo?: boolean
}

// ── Run staleness (shared by scripted and exploratory runs) ─────────

export type OutdatedReason = 'requirement' | 'test_plan' | 'test_environment'

// ── Test execution ──────────────────────────────────────────────────

export type TestExecutionStatus = 'pending' | 'running' | 'completed' | 'failed'

/** `skipped` = the execution finished without ever reaching this case. */
export type TestCaseExecutionStatus =
  'pending' | 'running' | 'passed' | 'failed' | 'error' | 'skipped'

export interface TestCaseExecutionResponse {
  id: number
  test_case: TestCaseResponse
  status: TestCaseExecutionStatus
  attempts: number
  output: string | null
  error: string | null
  /** Null unless the case ended in a terminal failure that recorded one. */
  finding: Finding | null
  updated_at: string
}

export interface TestExecutionResponse {
  id: number
  requirement_id: number
  requirement_name: string
  status: TestExecutionStatus
  error: string | null
  /**
   * Upstream artifacts that changed since the run executed. Empty means
   * current — there is no separate `outdated` field, since it would just be
   * `outdated_reasons.length > 0` and could disagree with this.
   */
  outdated_reasons: OutdatedReason[]
  /** Picks the wording for the `requirement` reason. Never a lone signal. */
  requirement_deleted: boolean
  cases: TestCaseExecutionResponse[]
  created_at: string
  updated_at: string
}

/** One filed ticket and how many of a run's findings it stands for. */
export interface TrackerIssueGroup {
  issue_key: string
  issue_url: string
  finding_count: number
}

/**
 * A run's export state, computed server-side and never stored.
 *
 * `export_error_count` is a **subset** of `unexported_finding_count`, not
 * disjoint from it: one decides whether the page offers the button, the
 * other words it.
 */
export interface ExportRollup {
  /**
   * The run's own start-time toggle — the one *stored* field here. The
   * button files either way, so this only changes the wording: it is what
   * separates "was set to file and has not yet" from "was never set to
   * file".
   */
  export_findings: boolean
  exported_finding_count: number
  exported_issue_count: number
  export_error_count: number
  unexported_finding_count: number
  export_groups: TrackerIssueGroup[]
}

export interface TestRunResponse extends ExportRollup {
  id: number
  sprint_id: number
  created_at: string
  status: TestExecutionStatus
  /**
   * Upstream artifacts that changed since the run executed. Empty means
   * current — there is no separate `outdated` field, since it would just be
   * `outdated_reasons.length > 0` and could disagree with this.
   */
  outdated_reasons: OutdatedReason[]
  /** Picks the wording for the `requirement` reason. Never a lone signal. */
  requirement_deleted: boolean
  requirement_names: string[]
  total_cases: number
  passed_cases: number
  failed_cases: number
  error_cases: number
}

export interface TestRunDetailResponse extends ExportRollup {
  id: number
  sprint_id: number
  created_at: string
  status: TestExecutionStatus
  /**
   * Upstream artifacts that changed since the run executed. Empty means
   * current — there is no separate `outdated` field, since it would just be
   * `outdated_reasons.length > 0` and could disagree with this.
   */
  outdated_reasons: OutdatedReason[]
  /** Picks the wording for the `requirement` reason. Never a lone signal. */
  requirement_deleted: boolean
  executions: TestExecutionResponse[]
}

// ── Exploratory testing ──────────────────────────────────────────────

export type ExploratoryRunStatus = 'pending' | 'running' | 'completed' | 'failed'

/** `skipped` = the run ended before this charter was ever explored. */
export type ExploratorySessionStatus = 'pending' | 'running' | 'completed' | 'error' | 'skipped'

export type SfdipotArea =
  'Structure' | 'Function' | 'Data' | 'Interfaces' | 'Platform' | 'Operations' | 'Time'

export const SFDIPOT_AREAS: SfdipotArea[] = [
  'Structure',
  'Function',
  'Data',
  'Interfaces',
  'Platform',
  'Operations',
  'Time',
]

export interface ExploratoryFindingResponse extends Finding {
  id: number
  position: number
  has_screenshot: boolean
  created_at: string
}

export interface ExploratorySessionSummaryResponse {
  id: number
  position: number
  charter: string
  sfdipot_areas: SfdipotArea[]
  status: ExploratorySessionStatus
  actions_used: number
  stop_reason: string | null
  error: string | null
  finding_count: number
  updated_at: string
}

export interface ExploratorySessionResponse {
  id: number
  exploratory_run_id: number
  position: number
  charter: string
  sfdipot_areas: SfdipotArea[]
  status: ExploratorySessionStatus
  actions_used: number
  session_notes: string | null
  action_log: string | null
  stop_reason: string | null
  error: string | null
  findings: ExploratoryFindingResponse[]
  updated_at: string
}

export interface ExploratoryRunResponse extends ExportRollup {
  id: number
  sprint_id: number
  requirement_id: number
  requirement_name: string
  status: ExploratoryRunStatus
  summary: string | null
  error: string | null
  outdated_reasons: OutdatedReason[]
  requirement_deleted: boolean
  session_count: number
  bug_count: number
  issue_count: number
  high_severity_count: number
  created_at: string
  updated_at: string
}

export interface ExploratoryRunDetailResponse extends ExportRollup {
  id: number
  sprint_id: number
  requirement_id: number
  requirement_name: string
  status: ExploratoryRunStatus
  summary: string | null
  error: string | null
  outdated_reasons: OutdatedReason[]
  requirement_deleted: boolean
  base_url_env_vars: string[]
  sessions: ExploratorySessionSummaryResponse[]
  bug_count: number
  issue_count: number
  high_severity_count: number
  created_at: string
  updated_at: string
}

export interface CharterDraft {
  charter: string
  sfdipot_areas: SfdipotArea[]
}

export interface ExploratoryCharterDraftResponse {
  requirement_id: number
  requirement_name: string
  charters: CharterDraft[]
  base_url_env_vars: string[]
  charter_count: number
  projected_minutes: number
}

// ── QA metrics ────────────────────────────────────────────────────────

export interface RequirementMetrics {
  requirement_id: number
  requirement_name: string
  requirement_deleted: boolean
  bug_count: number
  issue_count: number
  distinct_test_cases_run: number
  exploratory_sessions: number
}

/**
 * Mirrors `SprintMetricsResponse`. Every figure here is computed in
 * Python — nothing in this file may be re-derived from the others, for
 * the reason `test_plans_missing` exists: a definition that lives in two
 * places drifts, and the ticket collapse behind `bug_count` cannot be
 * done client-side at all.
 */
export interface SprintMetrics {
  sprint_id: number
  distinct_test_cases_run: number
  case_executions: number
  executions_passed: number
  executions_failed: number
  executions_errored: number
  exploratory_sessions: number
  requirements_explored: number
  bug_count: number
  issue_count: number
  high_severity_bug_count: number
  requirements_covered: number
  requirements_total: number
  /** Null when nothing was covered — render an em dash, never a zero. */
  bugs_per_requirement: number | null
  /** Null when no test case ran. */
  bugs_per_test_case: number | null
  per_requirement: RequirementMetrics[]
  excluded_runs_running: number
  excluded_runs_failed: number
}

// ── CI/CD export ──────────────────────────────────────────────────────

export type CicdProvider = 'github_actions' | 'jenkins'

export type CicdExportStatus = 'pending' | 'running' | 'completed' | 'failed'

/** Why a case cannot be exported. `null` when it can. */
export type CicdIneligibleReason = 'no_script' | 'stale'

export interface CicdConfig {
  id: number
  sprint_id: number
  provider: CicdProvider
  ci_environment_hint: string | null
  verified_at: string
  created_at: string
  updated_at: string
}

export interface CicdConfigInput {
  provider: CicdProvider
  /** Blank keeps whatever credential is already stored — a provider switch included. */
  access_token?: string | null
  ci_environment_hint?: string | null
}

export interface CicdCaseEntry {
  test_case_id: number
  case_title: string
  requirement_id: number
  requirement_name: string
  eligible: boolean
  reason: CicdIneligibleReason | null
  stale_reasons: string[]
  previously_exported: boolean
  last_export_pr_url: string | null
}

export interface CicdEligibility {
  sprint_id: number
  entries: CicdCaseEntry[]
  eligible_count: number
  stale_count: number
  no_script_count: number
  /** Names only — no environment value is ever serialized. */
  variable_names: string[]
  secret_names: string[]
}

export interface CicdExportItem {
  test_case_id: number
  case_title: string
  requirement_name: string
  committed_path: string
}

export interface CicdExport {
  id: number
  sprint_id: number
  provider: CicdProvider
  status: CicdExportStatus
  branch_name: string | null
  commit_sha: string | null
  pr_number: number | null
  pr_url: string | null
  pr_title: string | null
  notes: string | null
  error: string | null
  case_count: number
  ci_file_paths: string[]
  dropped_paths: string[]
  variable_names: string[]
  secret_names: string[]
  items: CicdExportItem[]
  created_at: string
  updated_at: string
}
