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
  requirements_locked: boolean
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
  updated_at: string
}

export interface TestExecutionResponse {
  id: number
  requirement_id: number
  requirement_name: string
  status: TestExecutionStatus
  error: string | null
  cases: TestCaseExecutionResponse[]
  created_at: string
  updated_at: string
}

export interface TestRunResponse {
  id: number
  sprint_id: number
  created_at: string
  status: TestExecutionStatus
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
  executions: TestExecutionResponse[]
}

// ── Exploratory testing ──────────────────────────────────────────────

export type ExploratoryRunStatus = 'pending' | 'running' | 'completed' | 'failed'

export type ExploratorySessionStatus = 'pending' | 'running' | 'completed' | 'error'

export type FindingType = 'bug' | 'issue'

export type FindingSeverity = 'high' | 'medium' | 'low'

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

export interface ExploratoryFindingResponse {
  id: number
  position: number
  finding_type: FindingType
  severity: FindingSeverity
  title: string
  steps_to_reproduce: string
  expected: string
  actual: string
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
