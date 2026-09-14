import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import NonfunctionalRunModal from './NonfunctionalRunModal'
import type { LoadProfileDraft, NonfunctionalPlanDraftResponse, TestPlanResponse } from '../types'
import { LOAD_LIMITS } from '../test/fixtures'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    generateNonfunctionalPlan: vi.fn(),
    createNonfunctionalRun: vi.fn(),
  }
})

import { createNonfunctionalRun, generateNonfunctionalPlan } from '../services/api'

const mockGenerate = generateNonfunctionalPlan as ReturnType<typeof vi.fn>
const mockCreate = createNonfunctionalRun as ReturnType<typeof vi.fn>

function makePlan(overrides: Partial<TestPlanResponse> = {}): TestPlanResponse {
  return {
    id: 1,
    requirement_id: 5,
    requirement_name: 'Login',
    status: 'approved',
    complexity: 'medium',
    summary: 's',
    revision_count: 0,
    feedback_cap_reached: false,
    pending_feedback: null,
    error: null,
    cases: [],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  } as TestPlanResponse
}

function makeProfile(overrides: Partial<LoadProfileDraft> = {}): LoadProfileDraft {
  return {
    url: 'https://app.test/api',
    method: 'GET',
    body: null,
    shape: 'load',
    concurrency: 1,
    duration_seconds: 10,
    total_request_cap: 50,
    rationale: 'hot path',
    ...overrides,
  }
}

function makeDraft(
  overrides: Partial<NonfunctionalPlanDraftResponse> = {},
): NonfunctionalPlanDraftResponse {
  return {
    requirement_id: 5,
    requirement_name: 'Login',
    domains: [
      { domain: 'accessibility', applicable: true, rationale: 'It has a UI.' },
      { domain: 'security', applicable: true, rationale: 'It is authenticated.' },
      { domain: 'performance', applicable: false, rationale: 'No load path.' },
    ],
    base_url_env_vars: ['BASE_URL'],
    load_profiles: [],
    ...overrides,
  }
}

function renderModal(plans: TestPlanResponse[] = [makePlan()]) {
  const router = createMemoryRouter(
    [
      {
        path: '*',
        element: (
          <NonfunctionalRunModal
            sprintId={1}
            plans={plans}
            limits={LOAD_LIMITS}
            onClose={() => {}}
          />
        ),
      },
    ],
    { initialEntries: ['/sprints/1/test-runs'] },
  )
  return render(<RouterProvider router={router} />)
}

async function reachTheReviewStep(draft = makeDraft()) {
  mockGenerate.mockResolvedValue(draft)
  renderModal()
  fireEvent.click(screen.getByRole('button', { name: 'Prepare run' }))
  await waitFor(() => expect(screen.getByText('Checks')).toBeInTheDocument())
}

const startButton = () => screen.getByRole('button', { name: /^Start run/ })
const input = (label: string) => screen.getByLabelText(label) as HTMLInputElement

describe('NonfunctionalRunModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockCreate.mockResolvedValue({ id: 42 })
  })

  it('offers the requirements with an approved plan', () => {
    renderModal([makePlan(), makePlan({ id: 2, requirement_id: 6, requirement_name: 'Search' })])

    expect(screen.getByText('Login')).toBeInTheDocument()
    expect(screen.getByText('Search')).toBeInTheDocument()
  })

  // ── Step 1: the ceiling, before any proposal exists ─────────────────

  it('asks for the ceiling first, pre-filled with and bounded by the server maximums', () => {
    renderModal()

    expect(input('Peak users').value).toBe('10')
    expect(input('Peak users').max).toBe('10')
    expect(input('Total requests for this run').value).toBe('2000')
    expect(input('Total requests for this run').max).toBe('2000')
    expect(screen.getByLabelText(/This environment is disposable/)).not.toBeChecked()
  })

  it('sends the ceiling and the declaration with Prepare', async () => {
    mockGenerate.mockResolvedValue(makeDraft())
    renderModal()

    fireEvent.change(input('Peak users'), { target: { value: '4' } })
    fireEvent.change(input('Total requests for this run'), { target: { value: '600' } })
    fireEvent.click(screen.getByLabelText(/This environment is disposable/))
    fireEvent.click(screen.getByRole('button', { name: 'Prepare run' }))

    await waitFor(() =>
      expect(mockGenerate).toHaveBeenCalledWith(
        1,
        5,
        { max_users: 4, max_total_requests: 600 },
        true,
      ),
    )
  })

  it('refuses to prepare with a ceiling outside the maximums, and says why', () => {
    renderModal()

    fireEvent.change(input('Peak users'), { target: { value: '11' } })

    expect(screen.getByRole('button', { name: 'Prepare run' })).toBeDisabled()
    expect(screen.getByText(/Peak users must be a whole number from 1 to 10/)).toBeInTheDocument()
  })

  // ── Step 2: review ──────────────────────────────────────────────────

  it('preselects the domains the server called applicable', async () => {
    await reachTheReviewStep()

    expect(screen.getByLabelText('Accessibility')).toBeChecked()
    expect(screen.getByLabelText('Security')).toBeChecked()
    // Proposed inapplicable — offered, but not preselected.
    expect(screen.getByLabelText('Performance')).not.toBeChecked()
    expect(screen.getByText('No load path.')).toBeInTheDocument()
  })

  it('makes non-safe methods unselectable until the declaration is set', async () => {
    await reachTheReviewStep(makeDraft({ load_profiles: [makeProfile()] }))

    const post = () => screen.getByRole('option', { name: /^POST/ }) as HTMLOptionElement
    expect(post().disabled).toBe(true)

    fireEvent.click(screen.getByLabelText(/This environment is disposable/))

    expect(post().disabled).toBe(false)
  })

  it('says why Start is disabled rather than only disabling it', async () => {
    await reachTheReviewStep()

    fireEvent.click(screen.getByLabelText('Accessibility'))
    fireEvent.click(screen.getByLabelText('Security'))

    expect(startButton()).toBeDisabled()
    expect(screen.getByText(/Select at least one check to run/)).toBeInTheDocument()
  })

  it('names the declaration when a non-safe method is what is blocking', async () => {
    await reachTheReviewStep(makeDraft({ load_profiles: [makeProfile()] }))

    fireEvent.click(screen.getByLabelText(/This environment is disposable/))
    fireEvent.change(screen.getByLabelText('Method for profile 1'), {
      target: { value: 'POST' },
    })
    fireEvent.click(screen.getByLabelText(/This environment is disposable/))

    expect(startButton()).toBeDisabled()
    expect(screen.getByText(/declare the environment disposable/)).toBeInTheDocument()
  })

  it('gives a declared non-safe method the same ceiling as GET', async () => {
    await reachTheReviewStep(makeDraft({ load_profiles: [makeProfile()] }))
    const getMax = input('Total requests').max

    fireEvent.click(screen.getByLabelText(/This environment is disposable/))
    fireEvent.change(screen.getByLabelText('Method for profile 1'), {
      target: { value: 'POST' },
    })

    // One tier: the declaration changes what is permitted, not the ceiling.
    expect(input('Total requests').max).toBe(getMax)
  })

  it('meters the allocation against the budget and blocks Start when it is over', async () => {
    await reachTheReviewStep(
      makeDraft({ load_profiles: [makeProfile({ total_request_cap: 1500 })] }),
    )

    expect(screen.getByText(/1,500 of 2,000 requests/)).toBeInTheDocument()
    expect(input('Total requests').max).toBe('2000')

    fireEvent.change(input('Total requests for this run'), { target: { value: '1000' } })

    expect(startButton()).toBeDisabled()
    expect(
      screen.getByText(/Load profiles request 1,500 in total; this run's budget is 1,000/),
    ).toBeInTheDocument()

    fireEvent.change(input('Total requests for this run'), { target: { value: '2000' } })

    expect(startButton()).toBeEnabled()
  })

  it('gives a stress profile no users of its own and blocks one that is too short', async () => {
    await reachTheReviewStep(
      makeDraft({ load_profiles: [makeProfile({ shape: 'stress', duration_seconds: 30 })] }),
    )

    expect(screen.queryByLabelText('Users')).not.toBeInTheDocument()
    expect(screen.getByText(/Ramps in steps up to 10 users/)).toBeInTheDocument()
    expect(input('Seconds').min).toBe('50')
    expect(startButton()).toBeDisabled()
    expect(screen.getByText(/A stress profile needs at least 50 seconds/)).toBeInTheDocument()

    fireEvent.change(input('Seconds'), { target: { value: '60' } })

    expect(startButton()).toBeEnabled()
  })

  it('gives a soak its own duration ceiling', async () => {
    await reachTheReviewStep(makeDraft({ load_profiles: [makeProfile({ shape: 'soak' })] }))

    expect(input('Seconds').max).toBe('900')

    fireEvent.change(screen.getByLabelText('Shape for profile 1'), { target: { value: 'load' } })

    expect(input('Seconds').max).toBe('60')
  })

  it('says that load profiles run authenticated', async () => {
    await reachTheReviewStep()

    expect(screen.getByText(/as the signed-in browser user/)).toBeInTheDocument()
  })

  it('sends what the user approved, with the ceiling and each shape', async () => {
    await reachTheReviewStep(makeDraft({ load_profiles: [makeProfile({ shape: 'spike' })] }))

    fireEvent.change(input('Peak users'), { target: { value: '8' } })
    fireEvent.click(startButton())

    await waitFor(() => expect(mockCreate).toHaveBeenCalled())
    expect(mockCreate).toHaveBeenCalledWith(
      1,
      5,
      ['accessibility', 'security'],
      ['BASE_URL'],
      [makeProfile({ shape: 'spike' })],
      false,
      { max_users: 8, max_total_requests: 2000 },
      false,
    )
  })

  it('surfaces a generate failure without leaving the first step', async () => {
    mockGenerate.mockRejectedValue(new Error('provider down'))
    renderModal()

    fireEvent.click(screen.getByRole('button', { name: 'Prepare run' }))

    await waitFor(() => expect(screen.getByText('provider down')).toBeInTheDocument())
    expect(screen.queryByText('Checks')).not.toBeInTheDocument()
  })
})
