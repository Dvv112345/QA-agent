import type {
  AuthCheckResponse,
  CharterDraft,
  ExploratoryCharterDraftResponse,
  ExploratoryRunDetailResponse,
  ExploratoryRunResponse,
  ExploratorySessionResponse,
  IssueTrackerConfig,
  IssueTrackerConfigInput,
  ReadmeStatusResponse,
  RepoResponse,
  RequirementInput,
  RequirementResponse,
  SprintResponse,
  TestEnvironmentResponse,
  TestExecutionResponse,
  TestPlanEditRequest,
  TestPlanResponse,
  TestRunDetailResponse,
  TestRunResponse,
} from '../types'

const API_BASE = import.meta.env.VITE_API_BASE

// ── Auth ─────────────────────────────────────────────────────────────

export async function checkAuthStatus(): Promise<AuthCheckResponse> {
  const response = await fetch(`${API_BASE}/api/auth/check`)
  if (!response.ok) {
    throw new Error(`Auth check failed (${response.status})`)
  }
  return response.json() as Promise<AuthCheckResponse>
}

export async function verifyPassword(password: string): Promise<AuthCheckResponse> {
  const response = await fetch(`${API_BASE}/api/auth/verify`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  })
  if (!response.ok) {
    let message = `Verification failed (${response.status})`
    try {
      const body = await response.json()
      if (body.detail) {
        if (typeof body.detail === 'string') message = body.detail
        else if (Array.isArray(body.detail))
          message = body.detail.map((e: { msg: string }) => e.msg).join('; ')
      }
    } catch {
      /* body wasn't JSON — keep default message */
    }
    throw new Error(message)
  }
  return response.json() as Promise<AuthCheckResponse>
}

// ── Repos ────────────────────────────────────────────────────────────

export async function createRepo(githubUrl: string, accessToken?: string): Promise<RepoResponse> {
  const formData = new FormData()
  formData.append('github_url', githubUrl)
  if (accessToken) formData.append('access_token', accessToken)

  const response = await fetch(`${API_BASE}/api/repos`, {
    method: 'POST',
    body: formData,
  })
  return handleResponse<RepoResponse>(response)
}

export async function fetchRepos(): Promise<RepoResponse[]> {
  const response = await fetch(`${API_BASE}/api/repos`)
  return handleResponse<RepoResponse[]>(response)
}

export async function deactivateRepo(id: number): Promise<void> {
  const response = await fetch(`${API_BASE}/api/repos/${id}/deactivate`, {
    method: 'POST',
  })
  await handleResponse(response)
}

export async function checkReadmeStatus(repoId: number): Promise<ReadmeStatusResponse> {
  const response = await fetch(`${API_BASE}/api/repos/${repoId}/readme-status`)
  return handleResponse<ReadmeStatusResponse>(response)
}

// ── Sprints ──────────────────────────────────────────────────────────

export async function createSprint(
  name: string,
  repoId: number,
  readmeFile?: File,
): Promise<SprintResponse> {
  const formData = new FormData()
  formData.append('name', name)
  formData.append('repo_id', String(repoId))
  if (readmeFile) formData.append('readme_file', readmeFile)

  const response = await fetch(`${API_BASE}/api/sprints`, {
    method: 'POST',
    body: formData,
  })
  return handleResponse<SprintResponse>(response)
}

export async function fetchSprints(): Promise<SprintResponse[]> {
  const response = await fetch(`${API_BASE}/api/sprints`)
  return handleResponse<SprintResponse[]>(response)
}

export async function fetchSprint(id: number): Promise<SprintResponse> {
  const response = await fetch(`${API_BASE}/api/sprints/${id}`)
  return handleResponse<SprintResponse>(response)
}

export async function finishSprint(id: number): Promise<SprintResponse> {
  const response = await fetch(`${API_BASE}/api/sprints/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ active: false }),
  })
  return handleResponse<SprintResponse>(response)
}

// ── Requirements ─────────────────────────────────────────────────────

export async function submitRequirements(
  sprintId: number,
  items: RequirementInput[],
): Promise<RequirementResponse[]> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/requirements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(items),
  })
  return handleResponse<RequirementResponse[]>(response)
}

export async function uploadPrd(sprintId: number, file: File): Promise<RequirementResponse[]> {
  const formData = new FormData()
  formData.append('prd_file', file)

  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/requirements/from-prd`, {
    method: 'POST',
    body: formData,
  })
  return handleResponse<RequirementResponse[]>(response)
}

export async function fetchRequirements(sprintId: number): Promise<RequirementResponse[]> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/requirements`)
  return handleResponse<RequirementResponse[]>(response)
}

export async function answerRequirement(id: number, answer: string): Promise<RequirementResponse> {
  const response = await fetch(`${API_BASE}/api/requirements/${id}/answer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answer }),
  })
  return handleResponse<RequirementResponse>(response)
}

export async function confirmRequirement(id: number): Promise<RequirementResponse> {
  const response = await fetch(`${API_BASE}/api/requirements/${id}/confirm`, {
    method: 'POST',
  })
  return handleResponse<RequirementResponse>(response)
}

export async function confirmAllRequirements(sprintId: number): Promise<RequirementResponse[]> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/requirements/confirm-all`, {
    method: 'POST',
  })
  return handleResponse<RequirementResponse[]>(response)
}

export async function updateRequirement(
  id: number,
  description: string,
): Promise<RequirementResponse> {
  const response = await fetch(`${API_BASE}/api/requirements/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ description }),
  })
  return handleResponse<RequirementResponse>(response)
}

export async function restartRequirement(id: number): Promise<RequirementResponse> {
  const response = await fetch(`${API_BASE}/api/requirements/${id}/restart`, {
    method: 'POST',
  })
  return handleResponse<RequirementResponse>(response)
}

export async function deleteRequirement(id: number): Promise<void> {
  const response = await fetch(`${API_BASE}/api/requirements/${id}`, {
    method: 'DELETE',
  })
  await handleResponse(response)
}

// ── Test environment ─────────────────────────────────────────────────

export async function fetchTestEnvironment(
  sprintId: number,
): Promise<TestEnvironmentResponse | null> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/test-environment`)
  if (response.status === 404) return null // no submission yet — not an error
  return handleResponse<TestEnvironmentResponse>(response)
}

export async function submitTestEnvironment(
  sprintId: number,
  content: string,
): Promise<TestEnvironmentResponse> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/test-environment`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
  return handleResponse<TestEnvironmentResponse>(response)
}

export async function answerTestEnvironment(
  id: number,
  answer: string,
): Promise<TestEnvironmentResponse> {
  const response = await fetch(`${API_BASE}/api/test-environment/${id}/answer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answer }),
  })
  return handleResponse<TestEnvironmentResponse>(response)
}

export async function confirmTestEnvironment(id: number): Promise<TestEnvironmentResponse> {
  const response = await fetch(`${API_BASE}/api/test-environment/${id}/confirm`, {
    method: 'POST',
  })
  return handleResponse<TestEnvironmentResponse>(response)
}

export async function updateTestEnvironmentVars(
  id: number,
  variables: Record<string, string>,
): Promise<TestEnvironmentResponse> {
  const response = await fetch(`${API_BASE}/api/test-environment/${id}/env-vars`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ variables }),
  })
  return handleResponse<TestEnvironmentResponse>(response)
}

// ── Test plans ───────────────────────────────────────────────────────

export async function generateTestPlans(sprintId: number): Promise<TestPlanResponse[]> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/test-plans/generate`, {
    method: 'POST',
  })
  return handleResponse<TestPlanResponse[]>(response)
}

export async function fetchTestPlans(sprintId: number): Promise<TestPlanResponse[]> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/test-plans`)
  return handleResponse<TestPlanResponse[]>(response)
}

export async function submitTestPlanFeedback(
  id: number,
  feedback: string,
): Promise<TestPlanResponse> {
  const response = await fetch(`${API_BASE}/api/test-plans/${id}/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ feedback }),
  })
  return handleResponse<TestPlanResponse>(response)
}

export async function updateTestPlan(
  id: number,
  body: TestPlanEditRequest,
): Promise<TestPlanResponse> {
  const response = await fetch(`${API_BASE}/api/test-plans/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return handleResponse<TestPlanResponse>(response)
}

export async function approveTestPlan(id: number): Promise<TestPlanResponse> {
  const response = await fetch(`${API_BASE}/api/test-plans/${id}/approve`, {
    method: 'POST',
  })
  return handleResponse<TestPlanResponse>(response)
}

export async function approveAllTestPlans(sprintId: number): Promise<TestPlanResponse[]> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/test-plans/approve-all`, {
    method: 'POST',
  })
  return handleResponse<TestPlanResponse[]>(response)
}

export async function restartTestPlan(id: number): Promise<TestPlanResponse> {
  const response = await fetch(`${API_BASE}/api/test-plans/${id}/restart`, {
    method: 'POST',
  })
  return handleResponse<TestPlanResponse>(response)
}

// ── Test execution ───────────────────────────────────────────────────

export async function createTestRun(
  sprintId: number,
  requirementIds: number[],
  exportFindings = false,
): Promise<TestRunDetailResponse> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/test-runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      requirement_ids: requirementIds,
      export_findings: exportFindings,
    }),
  })
  return handleResponse<TestRunDetailResponse>(response)
}

export async function fetchTestRuns(sprintId: number): Promise<TestRunResponse[]> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/test-runs`)
  return handleResponse<TestRunResponse[]>(response)
}

export async function fetchTestRun(runId: number): Promise<TestRunDetailResponse> {
  const response = await fetch(`${API_BASE}/api/test-runs/${runId}`)
  return handleResponse<TestRunDetailResponse>(response)
}

export async function restartTestExecution(executionId: number): Promise<TestExecutionResponse> {
  const response = await fetch(`${API_BASE}/api/test-executions/${executionId}/restart`, {
    method: 'POST',
  })
  return handleResponse<TestExecutionResponse>(response)
}

export function scriptDownloadUrl(caseExecutionId: number): string {
  return `${API_BASE}/api/test-case-executions/${caseExecutionId}/script`
}

// ── Helpers ──────────────────────────────────────────────────────────

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const body = await response.json()
      if (body.detail) {
        if (typeof body.detail === 'string') message = body.detail
        else if (Array.isArray(body.detail))
          message = body.detail.map((e: { msg: string }) => e.msg).join('; ')
      }
    } catch {
      /* body wasn't JSON — keep default message */
    }
    throw new Error(message)
  }
  if (response.status === 204 || response.headers.get('content-length') === '0') {
    return undefined as T
  }
  return response.json() as Promise<T>
}

// ── Exploratory testing ──────────────────────────────────────────────

export async function generateCharters(
  sprintId: number,
  requirementId: number,
): Promise<ExploratoryCharterDraftResponse> {
  const response = await fetch(
    `${API_BASE}/api/sprints/${sprintId}/exploratory-charters/generate`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requirement_id: requirementId }),
    },
  )
  return handleResponse<ExploratoryCharterDraftResponse>(response)
}

export async function createExploratoryRun(
  sprintId: number,
  requirementId: number,
  charters: CharterDraft[],
  baseUrlEnvVars: string[],
  exportFindings = false,
): Promise<ExploratoryRunDetailResponse> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/exploratory-runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      requirement_id: requirementId,
      charters,
      base_url_env_vars: baseUrlEnvVars,
      export_findings: exportFindings,
    }),
  })
  return handleResponse<ExploratoryRunDetailResponse>(response)
}

export async function fetchExploratoryRuns(sprintId: number): Promise<ExploratoryRunResponse[]> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/exploratory-runs`)
  return handleResponse<ExploratoryRunResponse[]>(response)
}

export async function fetchExploratoryRun(runId: number): Promise<ExploratoryRunDetailResponse> {
  const response = await fetch(`${API_BASE}/api/exploratory-runs/${runId}`)
  return handleResponse<ExploratoryRunDetailResponse>(response)
}

export async function fetchExploratorySession(
  sessionId: number,
): Promise<ExploratorySessionResponse> {
  const response = await fetch(`${API_BASE}/api/exploratory-sessions/${sessionId}`)
  return handleResponse<ExploratorySessionResponse>(response)
}

export async function restartExploratoryRun(runId: number): Promise<ExploratoryRunDetailResponse> {
  const response = await fetch(`${API_BASE}/api/exploratory-runs/${runId}/restart`, {
    method: 'POST',
  })
  return handleResponse<ExploratoryRunDetailResponse>(response)
}

export async function summarizeExploratoryRun(
  runId: number,
): Promise<ExploratoryRunDetailResponse> {
  const response = await fetch(`${API_BASE}/api/exploratory-runs/${runId}/summarize`, {
    method: 'POST',
  })
  return handleResponse<ExploratoryRunDetailResponse>(response)
}

export function findingScreenshotUrl(findingId: number): string {
  return `${API_BASE}/api/exploratory-findings/${findingId}/screenshot`
}

// ── Issue tracker ────────────────────────────────────────────────────

export async function fetchIssueTracker(sprintId: number): Promise<IssueTrackerConfig | null> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/issue-tracker`)
  return handleResponse<IssueTrackerConfig | null>(response)
}

export async function saveIssueTracker(
  sprintId: number,
  config: IssueTrackerConfigInput,
): Promise<IssueTrackerConfig> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/issue-tracker`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  })
  return handleResponse<IssueTrackerConfig>(response)
}

export async function deleteIssueTracker(sprintId: number): Promise<void> {
  const response = await fetch(`${API_BASE}/api/sprints/${sprintId}/issue-tracker`, {
    method: 'DELETE',
  })
  await handleResponse(response)
}

export async function exportTestRunFindings(runId: number): Promise<TestRunDetailResponse> {
  const response = await fetch(`${API_BASE}/api/test-runs/${runId}/export-findings`, {
    method: 'POST',
  })
  return handleResponse<TestRunDetailResponse>(response)
}

export async function exportExploratoryRunFindings(
  runId: number,
): Promise<ExploratoryRunDetailResponse> {
  const response = await fetch(`${API_BASE}/api/exploratory-runs/${runId}/export-findings`, {
    method: 'POST',
  })
  return handleResponse<ExploratoryRunDetailResponse>(response)
}
