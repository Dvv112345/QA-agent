import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import TestRunsPage from './TestRunsPage'
import type { SprintMetrics, SprintResponse, TestPlanResponse, TestRunResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchSprint: vi.fn(),
    fetchTestRuns: vi.fn(),
    fetchTestPlans: vi.fn(),
    createTestRun: vi.fn(),
    fetchExploratoryRuns: vi.fn(),
    fetchIssueTracker: vi.fn(),
    fetchSprintMetrics: vi.fn(),
    saveIssueTracker: vi.fn(),
    deleteIssueTracker: vi.fn(),
  }
})

import {
  createTestRun,
  fetchExploratoryRuns,
  fetchIssueTracker,
  fetchSprint,
  fetchSprintMetrics,
  fetchTestPlans,
  fetchTestRuns,
} from '../services/api'

const mockFetchSprint = fetchSprint as ReturnType<typeof vi.fn>
const mockFetchTestRuns = fetchTestRuns as ReturnType<typeof vi.fn>
const mockFetchTestPlans = fetchTestPlans as ReturnType<typeof vi.fn>
const mockCreateTestRun = createTestRun as ReturnType<typeof vi.fn>
const mockFetchExploratoryRuns = fetchExploratoryRuns as ReturnType<typeof vi.fn>
const mockFetchIssueTracker = fetchIssueTracker as ReturnType<typeof vi.fn>
const mockFetchSprintMetrics = fetchSprintMetrics as ReturnType<typeof vi.fn>

function makeMetrics(overrides: Partial<SprintMetrics> = {}): SprintMetrics {
  return {
    sprint_id: 1,
    distinct_test_cases_run: 0,
    case_executions: 0,
    executions_passed: 0,
    executions_failed: 0,
    executions_errored: 0,
    exploratory_sessions: 0,
    requirements_explored: 0,
    bug_count: 0,
    issue_count: 0,
    high_severity_bug_count: 0,
    requirements_covered: 0,
    requirements_total: 0,
    bugs_per_requirement: null,
    bugs_per_test_case: null,
    per_requirement: [],
    excluded_runs_running: 0,
    excluded_runs_failed: 0,
    ...overrides,
  }
}

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
    test_plans_missing: false,
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
    outdated_reasons: [],
    requirement_deleted: false,
    requirement_names: ['Login'],
    total_cases: 2,
    passed_cases: 2,
    failed_cases: 0,
    error_cases: 0,
    export_findings: false,
    exported_finding_count: 0,
    exported_issue_count: 0,
    export_error_count: 0,
    unexported_finding_count: 0,
    export_groups: [],
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
    mockFetchIssueTracker.mockResolvedValue(null)
    mockFetchSprintMetrics.mockResolvedValue(makeMetrics())
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

  it('keeps the run lists when the metrics endpoint fails', async () => {
    // The panel is decoration; the run lists are the page. A metrics
    // failure must cost the panel and nothing else — the frontend half of
    // the never-raise contract `services/qa_metrics.py` keeps server-side.
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockFetchTestRuns.mockResolvedValue([makeRun({ requirement_names: ['Login', 'Search'] })])
    mockFetchSprintMetrics.mockRejectedValue(new Error('metrics unavailable'))
    renderPage()

    expect(await screen.findByText('Login, Search')).toBeInTheDocument()
    expect(screen.queryByText('metrics unavailable')).not.toBeInTheDocument()
    expect(screen.queryByText('QA Metrics')).not.toBeInTheDocument()
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
    // The modal now holds two checkboxes — the requirement and the
    // export toggle — so this one is selected by name.
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Login' }))
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await waitFor(() => {
      expect(mockCreateTestRun).toHaveBeenCalledWith(1, [100], false)
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
    // clearAllMocks clears calls but keeps implementations, so a config
    // set by one test would otherwise leak into the next.
    mockFetchIssueTracker.mockResolvedValue(null)
    mockFetchSprintMetrics.mockResolvedValue(makeMetrics())
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
      outdated_reasons: [],
      requirement_deleted: false,
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
  describe('issue tracker panel', () => {
    it('offers to connect when nothing is configured', async () => {
      mockFetchSprint.mockResolvedValue(makeSprint())
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('No issue tracker connected.')).toBeInTheDocument()
      })
      expect(
        screen.getByRole('button', { name: 'Connect Jira or GitHub Issues' }),
      ).toBeInTheDocument()
    })

    it('names the connected tracker and offers to change it', async () => {
      mockFetchSprint.mockResolvedValue(makeSprint())
      mockFetchIssueTracker.mockResolvedValue({
        id: 1,
        sprint_id: 1,
        provider: 'jira',
        target: 'QA',
        target_label: 'Jira · QA',
        base_url: 'https://acme.atlassian.net',
        account_email: 'qa@acme.test',
        issue_type: 'Bug',
        verified_at: '2026-01-01T00:00:00Z',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      })
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Jira · QA')).toBeInTheDocument()
      })
      expect(screen.getByRole('button', { name: 'Change' })).toBeInTheDocument()
    })

    it('shows the panel even when the run lists are gated', async () => {
      // Connecting a tracker is sprint configuration, not a run action —
      // gating it behind approved plans would hide the setup step behind
      // the work it exists to serve.
      mockFetchSprint.mockResolvedValue(makeSprint({ test_plans_complete: false }))
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Approve every test plan first.')).toBeInTheDocument()
      })
      expect(screen.getByText('No issue tracker connected.')).toBeInTheDocument()
    })

    it('opens the modal on click', async () => {
      mockFetchSprint.mockResolvedValue(makeSprint())
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('No issue tracker connected.')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByRole('button', { name: 'Connect Jira or GitHub Issues' }))

      expect(screen.getByRole('dialog')).toBeInTheDocument()
      expect(screen.getByText('Connect an issue tracker')).toBeInTheDocument()
    })
  })
})

describe('TestRunsPage — QA metrics panel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockFetchTestRuns.mockResolvedValue([])
    mockFetchTestPlans.mockResolvedValue([])
    mockFetchExploratoryRuns.mockResolvedValue([])
    mockFetchIssueTracker.mockResolvedValue(null)
    mockFetchSprintMetrics.mockResolvedValue(makeMetrics())
  })

  it('renders the panel from the metrics endpoint', async () => {
    mockFetchTestRuns.mockResolvedValue([makeRun()])
    mockFetchSprintMetrics.mockResolvedValue(
      makeMetrics({
        distinct_test_cases_run: 20,
        case_executions: 60,
        bug_count: 9,
        requirements_covered: 5,
        requirements_total: 7,
        bugs_per_requirement: 1.8,
        bugs_per_test_case: 0.45,
      }),
    )
    renderPage()

    expect(await screen.findByText('QA Metrics')).toBeInTheDocument()
    expect(screen.getByText('60 executions')).toBeInTheDocument()
    expect(screen.getByText('5 requirements covered')).toBeInTheDocument()
    expect(screen.getByText('7 current requirements')).toBeInTheDocument()
    expect(screen.getByText('0.45 bugs / case')).toBeInTheDocument()
  })

  it('refetches the metrics on every poll tick, alongside the run lists', async () => {
    // Fake timers installed before render — an interval registered under
    // real timers cannot be advanced later.
    vi.useFakeTimers()
    try {
      mockFetchTestRuns.mockResolvedValue([makeRun({ status: 'running' })])
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(mockFetchSprintMetrics).toHaveBeenCalledTimes(1)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500)
      })

      expect(mockFetchSprintMetrics).toHaveBeenCalledTimes(2)
      expect(mockFetchTestRuns).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps polling a completed run whose bugs are not yet filed, then stops', async () => {
    // The gap this clause closes: export runs after the completion commit,
    // so the run reads terminal with every bug still unfiled. A poll
    // condition keyed purely on `running` would tear down inside that
    // window and freeze the panel until a reload.
    vi.useFakeTimers()
    try {
      const awaiting = makeRun({
        status: 'completed',
        export_findings: true,
        unexported_finding_count: 3,
        export_error_count: 0,
      })
      mockFetchTestRuns.mockResolvedValue([awaiting])
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500)
      })
      expect(mockFetchSprintMetrics).toHaveBeenCalledTimes(2)

      // The export lands: nothing is outstanding, so polling ends.
      mockFetchTestRuns.mockResolvedValue([
        makeRun({
          status: 'completed',
          export_findings: true,
          unexported_finding_count: 0,
          exported_finding_count: 3,
        }),
      ])
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500)
      })
      const settled = mockFetchSprintMetrics.mock.calls.length

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500 * 4)
      })
      expect(mockFetchSprintMetrics).toHaveBeenCalledTimes(settled)
    } finally {
      vi.useRealTimers()
    }
  })

  it('stops after the export grace budget rather than polling forever', async () => {
    vi.useFakeTimers()
    try {
      // A run stuck reading "not yet filed" — a tracker that never answers
      // must not leave this page polling for the rest of the session.
      mockFetchTestRuns.mockResolvedValue([
        makeRun({
          status: 'completed',
          export_findings: true,
          unexported_finding_count: 3,
          export_error_count: 0,
        }),
      ])
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500 * 60)
      })

      // 1 mount fetch + 48 graced ticks, and nothing after.
      expect(mockFetchSprintMetrics).toHaveBeenCalledTimes(49)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not poll a completed run whose filing failed', async () => {
    // A standing state, not a pending one: the export already ran and
    // wrote an error, so waiting changes nothing.
    vi.useFakeTimers()
    try {
      mockFetchTestRuns.mockResolvedValue([
        makeRun({
          status: 'completed',
          export_findings: true,
          unexported_finding_count: 3,
          export_error_count: 3,
        }),
      ])
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500 * 5)
      })

      expect(mockFetchSprintMetrics).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
