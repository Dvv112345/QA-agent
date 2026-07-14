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
  status: RequirementStatus
  clarifying_question: string | null
  revision_count: number
  clarification_cap_reached: boolean
  error: string | null
  created_at: string
  updated_at: string
}

export type TestEnvironmentStatus = 'needs_info' | 'ready' | 'confirmed'

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
  created_at: string
  updated_at: string
}
