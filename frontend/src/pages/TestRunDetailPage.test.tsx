import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import TestRunDetailPage from './TestRunDetailPage'
import type {
  SprintResponse,
  TestCaseExecutionResponse,
  TestExecutionResponse,
  TestRunDetailResponse,
} from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchSprint: vi.fn(),
    fetchTestRun: vi.fn(),
    restartTestExecution: vi.fn(),
  }
})

import { fetchSprint, fetchTestRun, restartTestExecution } from '../services/api'

const mockFetchSprint = fetchSprint as ReturnType<typeof vi.fn>
const mockFetchTestRun = fetchTestRun as ReturnType<typeof vi.fn>
const mockRestartTestExecution = restartTestExecution as ReturnType<typeof vi.fn>

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
    has_test_plans: true,
    test_plans_complete: true,
    has_test_runs: true,
    has_exploratory_runs: false,
    ...overrides,
  }
}

function makeCaseExecution(
  overrides: Partial<TestCaseExecutionResponse> = {},
): TestCaseExecutionResponse {
  return {
    id: 1,
    test_case: {
      id: 1,
      position: 0,
      title: 'Valid login',
      preconditions: null,
      steps: 'Open the login page\nSubmit valid credentials',
      expected_result: 'User lands on the dashboard.',
      case_type: 'functional',
      priority: 'high',
    },
    status: 'pending',
    attempts: 0,
    output: null,
    error: null,
    finding: null,
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeExecution(overrides: Partial<TestExecutionResponse> = {}): TestExecutionResponse {
  return {
    id: 1,
    requirement_id: 100,
    requirement_name: 'Login',
    status: 'running',
    error: null,
    cases: [makeCaseExecution()],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeRun(overrides: Partial<TestRunDetailResponse> = {}): TestRunDetailResponse {
  return {
    id: 1,
    sprint_id: 1,
    created_at: '2026-01-01T00:00:00Z',
    status: 'running',
    executions: [makeExecution()],
    ...overrides,
  }
}

function renderPage(sprintId = '1', runId = '1') {
  const router = createMemoryRouter(
    [{ path: '/sprints/:id/test-runs/:runId', element: <TestRunDetailPage /> }],
    { initialEntries: [`/sprints/${sprintId}/test-runs/${runId}`] },
  )
  return render(<RouterProvider router={router} />)
}

describe('TestRunDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders grouped executions and cases', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockFetchTestRun.mockResolvedValue(makeRun())
    renderPage()

    expect(await screen.findByText('Login')).toBeInTheDocument()
    expect(screen.getByText('Valid login')).toBeInTheDocument()
  })

  it('polls and updates case statuses while running', async () => {
    vi.useFakeTimers()
    try {
      mockFetchSprint.mockResolvedValue(makeSprint())
      mockFetchTestRun.mockResolvedValueOnce(
        makeRun({
          executions: [makeExecution({ cases: [makeCaseExecution({ status: 'running' })] })],
        }),
      )
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(screen.getAllByText('Running').length).toBeGreaterThan(0)

      mockFetchTestRun.mockResolvedValue(
        makeRun({
          status: 'completed',
          executions: [
            makeExecution({
              status: 'completed',
              cases: [makeCaseExecution({ status: 'passed', attempts: 1 })],
            }),
          ],
        }),
      )
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2600)
      })
      expect(screen.getByText('Passed')).toBeInTheDocument()

      const callsAfterSettle = mockFetchTestRun.mock.calls.length
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000)
      })
      expect(mockFetchTestRun.mock.calls.length).toBe(callsAfterSettle)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows Restart only on failed executions when sprint is active', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockFetchTestRun.mockResolvedValue(
      makeRun({
        status: 'failed',
        executions: [makeExecution({ status: 'failed', error: 'boom' })],
      }),
    )
    renderPage()

    expect(await screen.findByRole('button', { name: 'Restart' })).toBeInTheDocument()
    expect(screen.getByText('boom')).toBeInTheDocument()
  })

  it('hides Restart when the sprint is finished', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ active: false }))
    mockFetchTestRun.mockResolvedValue(
      makeRun({ status: 'failed', executions: [makeExecution({ status: 'failed' })] }),
    )
    renderPage()

    await screen.findByText('Login')
    expect(screen.queryByRole('button', { name: 'Restart' })).not.toBeInTheDocument()
  })

  it('restarts a failed execution', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockFetchTestRun.mockResolvedValue(
      makeRun({
        status: 'failed',
        executions: [makeExecution({ status: 'failed', error: 'boom' })],
      }),
    )
    mockRestartTestExecution.mockResolvedValue(makeExecution({ status: 'pending', error: null }))
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Restart' }))

    await waitFor(() => {
      expect(mockRestartTestExecution).toHaveBeenCalledWith(1)
    })
  })

  it('shows a download link once a case has finalized', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockFetchTestRun.mockResolvedValue(
      makeRun({
        executions: [
          makeExecution({ cases: [makeCaseExecution({ status: 'passed', attempts: 1 })] }),
        ],
      }),
    )
    renderPage()

    const link = await screen.findByRole('link', { name: 'Download script' })
    expect(link).toHaveAttribute('href', expect.stringContaining('/test-case-executions/1/script'))
  })

  it('does not show a download link for a pending case', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockFetchTestRun.mockResolvedValue(makeRun())
    renderPage()

    await screen.findByText('Valid login')
    expect(screen.queryByRole('link', { name: 'Download script' })).not.toBeInTheDocument()
  })
})
