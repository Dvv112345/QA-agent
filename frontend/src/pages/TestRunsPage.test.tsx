import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import TestRunsPage from './TestRunsPage'
import type { SprintResponse, TestPlanResponse, TestRunResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchSprint: vi.fn(),
    fetchTestRuns: vi.fn(),
    fetchTestPlans: vi.fn(),
    createTestRun: vi.fn(),
    fetchExploratoryRuns: vi.fn(),
  }
})

import {
  createTestRun,
  fetchExploratoryRuns,
  fetchSprint,
  fetchTestPlans,
  fetchTestRuns,
} from '../services/api'

const mockFetchSprint = fetchSprint as ReturnType<typeof vi.fn>
const mockFetchTestRuns = fetchTestRuns as ReturnType<typeof vi.fn>
const mockFetchTestPlans = fetchTestPlans as ReturnType<typeof vi.fn>
const mockCreateTestRun = createTestRun as ReturnType<typeof vi.fn>
const mockFetchExploratoryRuns = fetchExploratoryRuns as ReturnType<typeof vi.fn>

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
    environment_confirmed: true,
    has_test_plans: true,
    test_plans_complete: true,
    has_test_runs: false,
    has_exploratory_runs: false,
    ...overrides,
  }
}

function makeRun(overrides: Partial<TestRunResponse> = {}): TestRunResponse {
  return {
    id: 1,
    sprint_id: 1,
    created_at: '2026-01-01T00:00:00Z',
    status: 'completed',
    requirement_names: ['Login'],
    total_cases: 2,
    passed_cases: 2,
    failed_cases: 0,
    error_cases: 0,
    ...overrides,
  }
}

function makePlan(overrides: Partial<TestPlanResponse> = {}): TestPlanResponse {
  return {
    id: 10,
    requirement_id: 100,
    requirement_name: 'Login',
    requirement_description: 'Users can log in.',
    status: 'approved',
    complexity: 'medium',
    summary: 'Covers login.',
    revision_count: 0,
    feedback_cap_reached: false,
    error: null,
    cases: [],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPage(sprintId = '1') {
  const router = createMemoryRouter(
    [
      { path: '/sprints/:id/test-runs', element: <TestRunsPage /> },
      { path: '/sprints/:id/test-runs/:runId', element: <div>Run detail</div> },
    ],
    { initialEntries: [`/sprints/${sprintId}/test-runs`] },
  )
  return render(<RouterProvider router={router} />)
}

describe('TestRunsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchTestRuns.mockResolvedValue([])
    mockFetchTestPlans.mockResolvedValue([])
    mockFetchExploratoryRuns.mockResolvedValue([])
  })

  it('shows guard notice when ungated', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ test_plans_complete: false }))
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Approve every test plan first.')).toBeInTheDocument()
    })
    expect(screen.queryByRole('button', { name: 'Run new test' })).not.toBeInTheDocument()
  })

  it('shows finished notice on inactive sprint with no runs', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ active: false }))
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('This sprint is finished.')).toBeInTheDocument()
    })
  })

  it('renders run rows with joined requirement names and counts', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockFetchTestRuns.mockResolvedValue([
      makeRun({ requirement_names: ['Login', 'Search'], passed_cases: 3, failed_cases: 1 }),
    ])
    renderPage()

    expect(await screen.findByText('Login, Search')).toBeInTheDocument()
    expect(screen.getByText('3 passed / 1 failed')).toBeInTheDocument()
  })

  it('opens the modal on "Run new test"', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockFetchTestPlans.mockResolvedValue([makePlan()])
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Run new test' }))

    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('Login')).toBeInTheDocument()
  })

  it('polling advances a running row to completed and stops', async () => {
    vi.useFakeTimers()
    try {
      mockFetchSprint.mockResolvedValue(makeSprint({ has_test_runs: true }))
      mockFetchTestRuns.mockResolvedValueOnce([makeRun({ status: 'running' })])
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(screen.getByText('Running')).toBeInTheDocument()

      mockFetchTestRuns.mockResolvedValue([makeRun({ status: 'completed' })])
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2600)
      })
      expect(screen.getByText('Completed')).toBeInTheDocument()

      const callsAfterSettle = mockFetchTestRuns.mock.calls.length
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000)
      })
      expect(mockFetchTestRuns.mock.calls.length).toBe(callsAfterSettle)
    } finally {
      vi.useRealTimers()
    }
  })

  it('creates a run and navigates via the modal', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockFetchTestPlans.mockResolvedValue([makePlan()])
    mockCreateTestRun.mockResolvedValue({
      id: 42,
      sprint_id: 1,
      created_at: '2026-01-01T00:00:00Z',
      status: 'running',
      executions: [],
    })
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Run new test' }))
    fireEvent.click(await screen.findByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await waitFor(() => {
      expect(mockCreateTestRun).toHaveBeenCalledWith(1, [100])
    })
  })
})

describe('TestRunsPage — exploratory list', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchTestRuns.mockResolvedValue([])
    mockFetchTestPlans.mockResolvedValue([])
    mockFetchExploratoryRuns.mockResolvedValue([])
    mockFetchSprint.mockResolvedValue(makeSprint())
  })

  function makeExploratoryRun(overrides = {}) {
    return {
      id: 5,
      sprint_id: 1,
      requirement_id: 11,
      requirement_name: 'Export reports',
      status: 'completed' as const,
      summary: 'Looks sound.',
      error: null,
      session_count: 3,
      bug_count: 2,
      issue_count: 1,
      high_severity_count: 1,
      created_at: '2026-07-28T00:00:00Z',
      updated_at: '2026-07-28T00:00:00Z',
      ...overrides,
    }
  }

  it('renders both section headings', async () => {
    renderPage()

    expect(await screen.findByText('Exploratory Sessions')).toBeInTheDocument()
    expect(screen.getByText('Scripted Test Runs')).toBeInTheDocument()
  })

  it('renders both empty states independently', async () => {
    renderPage()

    expect(await screen.findByText('No exploratory runs yet.')).toBeInTheDocument()
    expect(screen.getByText('No test runs yet.')).toBeInTheDocument()
  })

  it('shows an exploratory run with its finding summary', async () => {
    mockFetchExploratoryRuns.mockResolvedValue([makeExploratoryRun()])
    renderPage()

    expect(await screen.findByText('Export reports')).toBeInTheDocument()
    expect(screen.getByText('2 bugs / 1 issue')).toBeInTheDocument()
  })

  it('says "No findings" for a clean completed run', async () => {
    mockFetchExploratoryRuns.mockResolvedValue([
      makeExploratoryRun({ bug_count: 0, issue_count: 0 }),
    ])
    renderPage()

    expect(await screen.findByText('No findings')).toBeInTheDocument()
  })

  it('links an exploratory run to its detail page', async () => {
    mockFetchExploratoryRuns.mockResolvedValue([makeExploratoryRun()])
    renderPage()

    const link = await screen.findByRole('link', { name: /Export reports/ })
    expect(link).toHaveAttribute('href', '/sprints/1/exploratory-runs/5')
  })

  it('opens the charter modal from the exploratory button', async () => {
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Start exploratory testing' }))

    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })

  it('keeps the page reachable when only exploratory runs exist', async () => {
    // Runs outlive test_plans_complete going false again — the guard keys on
    // the absence of *both* run types.
    mockFetchSprint.mockResolvedValue(makeSprint({ test_plans_complete: false }))
    mockFetchExploratoryRuns.mockResolvedValue([makeExploratoryRun()])
    renderPage()

    expect(await screen.findByText('Exploratory Sessions')).toBeInTheDocument()
    expect(screen.queryByText('Approve every test plan first.')).not.toBeInTheDocument()
  })

  it('polls while an exploratory run is in progress, then stops', async () => {
    // Fake timers must be installed before render — an interval registered
    // under real timers can't be advanced later.
    vi.useFakeTimers()
    try {
      mockFetchExploratoryRuns.mockResolvedValueOnce([makeExploratoryRun({ status: 'running' })])
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(screen.getByText('Exploring')).toBeInTheDocument()

      mockFetchExploratoryRuns.mockResolvedValue([makeExploratoryRun({ status: 'completed' })])
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2600)
      })
      expect(screen.getByText('Completed')).toBeInTheDocument()

      const callsAfterSettle = mockFetchExploratoryRuns.mock.calls.length
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000)
      })
      expect(mockFetchExploratoryRuns.mock.calls.length).toBe(callsAfterSettle)
    } finally {
      vi.useRealTimers()
    }
  })
})
