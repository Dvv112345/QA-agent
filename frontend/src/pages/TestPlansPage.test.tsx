import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import TestPlansPage from './TestPlansPage'
import type { SprintResponse, TestPlanResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchSprint: vi.fn(),
    fetchTestPlans: vi.fn(),
    generateTestPlans: vi.fn(),
    finishSprint: vi.fn(),
  }
})

import { fetchSprint, fetchTestPlans, finishSprint, generateTestPlans } from '../services/api'

const mockFetchSprint = fetchSprint as ReturnType<typeof vi.fn>
const mockFetchTestPlans = fetchTestPlans as ReturnType<typeof vi.fn>
const mockGenerateTestPlans = generateTestPlans as ReturnType<typeof vi.fn>
const mockFinishSprint = finishSprint as ReturnType<typeof vi.fn>

function makeSprint(overrides: Partial<SprintResponse> = {}): SprintResponse {
  return {
    id: 1,
    name: 'Sprint 1',
    repo_id: 1,
    active: true,
    directory: 'abc123',
    created_at: '2026-01-01T00:00:00Z',
    repo: null,
    requirements_complete: true,
    has_test_environment_submission: true,
    requirements_locked: true,
    has_test_plans: false,
    test_plans_complete: false,
    has_test_runs: false,
    ...overrides,
  }
}

function makePlan(overrides: Partial<TestPlanResponse> = {}): TestPlanResponse {
  return {
    id: 10,
    requirement_id: 100,
    requirement_name: 'Login',
    requirement_description: 'Users can log in.',
    status: 'draft',
    complexity: 'medium',
    summary: 'Covers the login flows.',
    revision_count: 0,
    feedback_cap_reached: false,
    error: null,
    cases: [
      {
        id: 1,
        position: 0,
        title: 'Valid login',
        preconditions: null,
        steps: 'Open the login page\nSubmit valid credentials',
        expected_result: 'User lands on the dashboard.',
        case_type: 'functional',
        priority: 'high',
      },
    ],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPage(sprintId = '1') {
  const router = createMemoryRouter(
    [{ path: '/sprints/:id/test-plans', element: <TestPlansPage /> }],
    { initialEntries: [`/sprints/${sprintId}/test-plans`] },
  )
  return render(<RouterProvider router={router} />)
}

describe('TestPlansPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchTestPlans.mockResolvedValue([])
  })

  it('shows guard notice when the test environment is not confirmed', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ requirements_locked: false }))
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Confirm the test environment first.')).toBeInTheDocument()
    })
    expect(screen.queryByRole('button', { name: 'Generate test plans' })).not.toBeInTheDocument()
  })

  it('shows finished notice on inactive sprint with no plans', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ active: false }))
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('This sprint is finished.')).toBeInTheDocument()
    })
  })

  it('has back links to sprints and the test environment', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    renderPage()

    const sprints = await screen.findByRole('link', { name: /back to sprints/i })
    expect(sprints).toHaveAttribute('href', '/')
    expect(screen.getByRole('link', { name: /back to test environment/i })).toHaveAttribute(
      'href',
      '/sprints/1/test-environment',
    )
  })

  it('generates plans and renders the returned list', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockGenerateTestPlans.mockResolvedValue([makePlan({ status: 'pending', cases: [] })])
    renderPage()

    const button = await screen.findByRole('button', { name: 'Generate test plans' })
    fireEvent.click(button)

    await waitFor(() => {
      expect(mockGenerateTestPlans).toHaveBeenCalledWith(1)
    })
    expect(screen.getByText('Login')).toBeInTheDocument()
    expect(screen.getByText('Queued')).toBeInTheDocument()
  })

  it('surfaces generate errors', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockGenerateTestPlans.mockRejectedValue(new Error('Confirm the test environment first!'))
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Generate test plans' }))

    expect(await screen.findByText('Confirm the test environment first!')).toBeInTheDocument()
  })

  it('polls while a plan is in progress and stops when settled', async () => {
    vi.useFakeTimers()
    try {
      mockFetchSprint.mockResolvedValue(makeSprint({ has_test_plans: true }))
      mockFetchTestPlans.mockResolvedValueOnce([makePlan({ status: 'generating', cases: [] })])
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(screen.getByText('Generating')).toBeInTheDocument()

      mockFetchTestPlans.mockResolvedValue([makePlan({ status: 'draft' })])
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2600)
      })
      expect(screen.getByText('Draft')).toBeInTheDocument()

      const callsAfterSettle = mockFetchTestPlans.mock.calls.length
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000)
      })
      expect(mockFetchTestPlans.mock.calls.length).toBe(callsAfterSettle)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows the summary line and completion banner', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_plans: true }))
    mockFetchTestPlans.mockResolvedValue([
      makePlan({ status: 'approved' }),
      makePlan({ id: 11, requirement_id: 101, requirement_name: 'Search', status: 'approved' }),
    ])
    renderPage()

    expect(await screen.findByText('2 of 2 plans drafted · 2 approved')).toBeInTheDocument()
    expect(screen.getByText('All test plans approved.')).toBeInTheDocument()
  })

  it('hides the Continue to Test Runs link while any plan is unapproved', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_plans: true }))
    mockFetchTestPlans.mockResolvedValue([
      makePlan({ status: 'draft' }),
      makePlan({ id: 11, requirement_id: 101, requirement_name: 'Search', status: 'approved' }),
    ])
    renderPage()

    await screen.findByText('2 of 2 plans drafted · 1 approved')
    expect(screen.queryByRole('link', { name: 'Continue to Test Runs' })).not.toBeInTheDocument()
  })

  it('shows the Continue to Test Runs link once every plan is approved', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_plans: true }))
    mockFetchTestPlans.mockResolvedValue([
      makePlan({ status: 'approved' }),
      makePlan({ id: 11, requirement_id: 101, requirement_name: 'Search', status: 'approved' }),
    ])
    renderPage()

    const link = await screen.findByRole('link', { name: 'Continue to Test Runs' })
    expect(link).toHaveAttribute('href', '/sprints/1/test-runs')
  })

  it('finishes the sprint from the footer', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_plans: true }))
    mockFetchTestPlans.mockResolvedValue([makePlan()])
    mockFinishSprint.mockResolvedValue(makeSprint({ active: false, has_test_plans: true }))
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Finish Sprint' }))

    await waitFor(() => {
      expect(mockFinishSprint).toHaveBeenCalledWith(1)
    })
    expect(screen.queryByRole('button', { name: 'Finish Sprint' })).not.toBeInTheDocument()
  })
})
