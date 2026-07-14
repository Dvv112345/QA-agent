import type {
  AuthCheckResponse,
  ReadmeStatusResponse,
  RepoResponse,
  RequirementInput,
  RequirementResponse,
  SprintResponse,
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
