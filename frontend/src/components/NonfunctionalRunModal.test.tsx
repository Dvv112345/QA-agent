import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import NonfunctionalRunModal from './NonfunctionalRunModal'
import type { NonfunctionalPlanDraftResponse, TestPlanResponse } from '../types'

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
    max_concurrency: 10,
    max_duration_seconds: 60,
    max_total_requests: 2000,
    unsafe_max_concurrency: 2,
    unsafe_max_total_requests: 20,
    safe_methods: ['GET', 'HEAD', 'OPTIONS'],
    ...overrides,
  }
}

function renderModal(plans: TestPlanResponse[] = [makePlan()]) {
  const router = createMemoryRouter(
    [
      {
        path: '*',
        element: <NonfunctionalRunModal sprintId={1} plans={plans} onClose={() => {}} />,
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

  it('preselects the domains the server called applicable', async () => {
    await reachTheReviewStep()

    expect(screen.getByLabelText('Accessibility')).toBeChecked()
    expect(screen.getByLabelText('Security')).toBeChecked()
    // Proposed inapplicable — offered, but not preselected.
    expect(screen.getByLabelText('Performance')).not.toBeChecked()
    expect(screen.getByText('No load path.')).toBeInTheDocument()
  })

  it('makes non-safe methods unselectable until the declaration is set', async () => {
    await reachTheReviewStep(
      makeDraft({
        load_profiles: [
          {
            url: 'https://app.test/api',
            method: 'GET',
            body: null,
            concurrency: 1,
            duration_seconds: 10,
            total_request_cap: 50,
            rationale: 'hot path',
          },
        ],
      }),
    )

    const method = screen.getByLabelText('Method for profile 1')
    const post = () => screen.getByRole('option', { name: /^POST/ }) as HTMLOptionElement
    expect(post().disabled).toBe(true)

    fireEvent.click(screen.getByLabelText(/This environment is disposable/))

    expect(post().disabled).toBe(false)
    expect(method).toBeInTheDocument()
  })

  it('says why Start is disabled rather than only disabling it', async () => {
    // The only other signal is a suffix inside a <select> the user has
    // already closed, so a disabled button looks broken instead of gated.
    await reachTheReviewStep()

    fireEvent.click(screen.getByLabelText('Accessibility'))
    fireEvent.click(screen.getByLabelText('Security'))

    expect(screen.getByRole('button', { name: /^Start run/ })).toBeDisabled()
    expect(screen.getByText(/Select at least one check to run/)).toBeInTheDocument()
  })

  it('names the declaration when a non-safe method is what is blocking', async () => {
    await reachTheReviewStep(
      makeDraft({
        load_profiles: [
          {
            url: 'https://app.test/api',
            method: 'GET',
            body: null,
            concurrency: 1,
            duration_seconds: 10,
            total_request_cap: 50,
            rationale: 'hot path',
          },
        ],
      }),
    )

    fireEvent.click(screen.getByLabelText(/This environment is disposable/))
    fireEvent.change(screen.getByLabelText('Method for profile 1'), {
      target: { value: 'POST' },
    })
    fireEvent.click(screen.getByLabelText(/This environment is disposable/))

    expect(screen.getByRole('button', { name: /^Start run/ })).toBeDisabled()
    expect(screen.getByText(/declare the environment disposable/)).toBeInTheDocument()
  })

  it('shows the ceiling for the tier a profile is in, from the server', async () => {
    await reachTheReviewStep(
      makeDraft({
        load_profiles: [
          {
            url: 'https://app.test/api',
            method: 'GET',
            body: null,
            concurrency: 1,
            duration_seconds: 10,
            total_request_cap: 50,
            rationale: '',
          },
        ],
      }),
    )

    expect(screen.getByText('max 10 × 2000')).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText(/This environment is disposable/))
    fireEvent.change(screen.getByLabelText('Method for profile 1'), {
      target: { value: 'POST' },
    })

    // The unsafe tier, and both figures came from the response — no config
    // literal is restated in this component.
    expect(screen.getByText('max 2 × 20')).toBeInTheDocument()
  })

  it('says that load profiles run authenticated', async () => {
    await reachTheReviewStep()

    expect(screen.getByText(/as the signed-in browser user/)).toBeInTheDocument()
  })

  it('refuses to start with no domain selected', async () => {
    await reachTheReviewStep()

    fireEvent.click(screen.getByLabelText('Accessibility'))
    fireEvent.click(screen.getByLabelText('Security'))

    expect(screen.getByRole('button', { name: /Start run/ })).toBeDisabled()
  })

  it('sends what the user approved', async () => {
    await reachTheReviewStep()

    fireEvent.click(screen.getByRole('button', { name: /Start run/ }))

    await waitFor(() => expect(mockCreate).toHaveBeenCalled())
    expect(mockCreate).toHaveBeenCalledWith(
      1,
      5,
      ['accessibility', 'security'],
      ['BASE_URL'],
      [],
      false,
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
