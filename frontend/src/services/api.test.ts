/**
 * Tests for the API client.
 *
 * Every function here is a thin `fetch(url).then(handleResponse)` wrapper, so
 * a "returns the parsed body on 200" test only asserts that the mock echoes
 * back what the mock was told to return. What is genuinely this module's own
 * behaviour is `handleResponse`'s error parsing (FastAPI `detail` arrives as
 * a string *or* as `[{ msg }]` — Convention #6) and the request bodies the
 * wrappers build.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { checkAuthStatus, createRepo, createSprint, finishSprint, verifyPassword } from './api'
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
  has_access_token: false,
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
  has_nonfunctional_runs: false,
}

// ── Error handling ───────────────────────────────────────────────────

describe('handleResponse', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('throws with the status when the body carries no detail', async () => {
    mockFetch(new Response('Internal error', { status: 500 }))
    await expect(checkAuthStatus()).rejects.toThrow('Auth check failed (500)')
  })

  it('surfaces a string FastAPI detail', async () => {
    mockFetch(new Response(JSON.stringify({ detail: 'Invalid URL' }), { status: 422 }))
    await expect(createRepo('bad-url')).rejects.toThrow('Invalid URL')
  })

  it('surfaces a validation-error detail array', async () => {
    mockFetch(
      new Response(JSON.stringify({ detail: [{ msg: 'field required' }] }), { status: 422 }),
    )
    await expect(createRepo('bad-url')).rejects.toThrow('field required')
  })
})

// ── Request bodies ───────────────────────────────────────────────────

describe('request construction', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('verifyPassword sends POST with the password as JSON', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ valid: true }), { status: 200 }))

    await verifyPassword('secret123')

    const [, options] = fetchSpy.mock.calls[0]
    expect(options?.method).toBe('POST')
    expect(options?.body).toBe(JSON.stringify({ password: 'secret123' }))
  })

  it('createSprint sends FormData with name, repo_id and the README file', async () => {
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

  it('finishSprint sends PATCH with active=false', async () => {
    const finished = { ...fakeSprint, active: false }
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify(finished), { status: 200 }))

    const result = await finishSprint(1)

    const [, options] = fetchSpy.mock.calls[0]
    expect(options?.method).toBe('PATCH')
    expect(options?.body).toBe(JSON.stringify({ active: false }))
    expect(result).toEqual(finished)
  })
})
