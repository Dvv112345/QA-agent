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
}

// ── Run staleness (shared by scripted and exploratory runs) ─────────

export type OutdatedReason = 'requirement' | 'test_plan' | 'test_environment'

// ── Test execution ──────────────────────────────────────────────────

export type TestExecutionStatus = 'pending' | 'running' | 'completed' | 'failed'

export type TestCaseExecutionStatus = 'pending' | 'running' | 'passed' | 'failed' | 'error'

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

export interface TestRunResponse {
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

export interface TestRunDetailResponse {
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

export type ExploratorySessionStatus = 'pending' | 'running' | 'completed' | 'error'

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

export interface ExploratoryRunResponse {
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

export interface ExploratoryRunDetailResponse {
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
