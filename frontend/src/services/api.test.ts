import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  checkAuthStatus,
  checkReadmeStatus,
  createRepo,
  createSprint,
  deactivateRepo,
  fetchRepos,
  fetchSprint,
  fetchSprints,
  finishSprint,
  verifyPassword,
} from './api'
import type { RepoResponse, SprintResponse } from '../types'

function mockFetch(response: Response) {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(response)
}

const fakeRepo: RepoResponse = {
  id: 1,
  github_link: 'https://github.com/owner/repo',
  name: 'owner/repo',
  description: 'A test repo',
  active: true,
  created_at: '2026-01-01T00:00:00Z',
}

const fakeSprint: SprintResponse = {
  id: 1,
  name: 'Sprint 1',
  repo_id: 1,
  active: true,
  directory: 'abc123',
  created_at: '2026-01-01T00:00:00Z',
  repo: fakeRepo,
  requirements_complete: false,
  has_test_environment_submission: false,
  environment_confirmed: false,
  has_test_plans: false,
  test_plans_missing: false,
  test_plans_complete: false,
  has_test_runs: false,
  has_exploratory_runs: false,
}

// ── Auth ─────────────────────────────────────────────────────────────

describe('checkAuthStatus', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('returns AuthCheckResponse with valid=true on 200', async () => {
    mockFetch(new Response(JSON.stringify({ valid: true }), { status: 200 }))
    const result = await checkAuthStatus()
    expect(result).toEqual({ valid: true })
  })

  it('throws on non-200 response', async () => {
    mockFetch(new Response('Internal error', { status: 500 }))
    await expect(checkAuthStatus()).rejects.toThrow('Auth check failed (500)')
  })
})

describe('verifyPassword', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('sends POST with correct JSON body', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ valid: true }), { status: 200 }))

    await verifyPassword('secret123')

    const [, options] = fetchSpy.mock.calls[0]
    expect(options?.method).toBe('POST')
    expect(options?.body).toBe(JSON.stringify({ password: 'secret123' }))
  })
})

// ── Repos ────────────────────────────────────────────────────────────

describe('createRepo', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('returns RepoResponse on 201', async () => {
    mockFetch(new Response(JSON.stringify(fakeRepo), { status: 201 }))
    const result = await createRepo('https://github.com/owner/repo')
    expect(result).toEqual(fakeRepo)
  })

  it('throws on error response', async () => {
    mockFetch(new Response(JSON.stringify({ detail: 'Invalid URL' }), { status: 422 }))
    await expect(createRepo('bad-url')).rejects.toThrow('Invalid URL')
  })
})

describe('fetchRepos', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('returns repo list on 200', async () => {
    mockFetch(new Response(JSON.stringify([fakeRepo]), { status: 200 }))
    const result = await fetchRepos()
    expect(result).toEqual([fakeRepo])
  })
})

describe('deactivateRepo', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('resolves on 200', async () => {
    mockFetch(new Response(JSON.stringify({ deactivated: true }), { status: 200 }))
    await expect(deactivateRepo(1)).resolves.toBeUndefined()
  })
})

describe('checkReadmeStatus', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('returns has_readme true', async () => {
    mockFetch(new Response(JSON.stringify({ has_readme: true }), { status: 200 }))
    const result = await checkReadmeStatus(1)
    expect(result).toEqual({ has_readme: true })
  })
})

// ── Sprints ──────────────────────────────────────────────────────────

describe('createSprint', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('returns SprintResponse on 201', async () => {
    mockFetch(new Response(JSON.stringify(fakeSprint), { status: 201 }))
    const result = await createSprint('Sprint 1', 1)
    expect(result).toEqual(fakeSprint)
  })

  it('sends FormData with name and repo_id', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify(fakeSprint), { status: 201 }))

    const readmeFile = new File(['# README'], 'README.md', { type: 'text/markdown' })
    await createSprint('My Sprint', 42, readmeFile)

    const [, options] = fetchSpy.mock.calls[0]
    const body = options?.body as FormData
    expect(body.get('name')).toBe('My Sprint')
    expect(body.get('repo_id')).toBe('42')
    expect(body.get('readme_file')).toBe(readmeFile)
  })
})

describe('fetchSprints', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('returns sprint list on 200', async () => {
    mockFetch(new Response(JSON.stringify([fakeSprint]), { status: 200 }))
    const result = await fetchSprints()
    expect(result).toEqual([fakeSprint])
  })
})

describe('fetchSprint', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('returns single sprint on 200', async () => {
    mockFetch(new Response(JSON.stringify(fakeSprint), { status: 200 }))
    const result = await fetchSprint(1)
    expect(result).toEqual(fakeSprint)
  })
})

describe('finishSprint', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('sends PATCH with active=false', async () => {
    const finished = { ...fakeSprint, active: false }
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify(finished), { status: 200 }))

    const result = await finishSprint(1)
    expect(result).toEqual(finished)

    const [, options] = fetchSpy.mock.calls[0]
    expect(options?.method).toBe('PATCH')
    expect(options?.body).toBe(JSON.stringify({ active: false }))
  })
})
