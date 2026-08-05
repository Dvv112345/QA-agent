import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import ExploratoryRunDetailPage from './ExploratoryRunDetailPage'
import type { ExploratoryRunDetailResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchExploratoryRun: vi.fn(),
    restartExploratoryRun: vi.fn(),
    summarizeExploratoryRun: vi.fn(),
    exportExploratoryRunFindings: vi.fn(),
  }
})

import {
  exportExploratoryRunFindings,
  fetchExploratoryRun,
  restartExploratoryRun,
  summarizeExploratoryRun,
} from '../services/api'

const mockFetchRun = fetchExploratoryRun as ReturnType<typeof vi.fn>
const mockRestart = restartExploratoryRun as ReturnType<typeof vi.fn>
const mockSummarize = summarizeExploratoryRun as ReturnType<typeof vi.fn>
const mockExport = exportExploratoryRunFindings as ReturnType<typeof vi.fn>

function makeRun(
  overrides: Partial<ExploratoryRunDetailResponse> = {},
): ExploratoryRunDetailResponse {
  return {
    id: 5,
    sprint_id: 1,
    requirement_id: 11,
    requirement_name: 'Export reports',
    status: 'completed',
    summary: 'Export is broadly sound.',
    error: null,
    outdated_reasons: [],
    requirement_deleted: false,
    base_url_env_vars: ['APP_URL'],
    sessions: [
      {
        id: 21,
        position: 0,
        charter: 'Explore export edge data',
        sfdipot_areas: ['Data'],
        status: 'completed',
        actions_used: 18,
        stop_reason: 'charter_complete',
        error: null,
        finding_count: 2,
        updated_at: '2026-07-28T00:00:00Z',
      },
    ],
    bug_count: 1,
    issue_count: 1,
    high_severity_count: 1,
    export_findings: false,
    exported_finding_count: 0,
    exported_issue_count: 0,
    export_error_count: 0,
    unexported_finding_count: 0,
    export_groups: [],
    created_at: '2026-07-28T00:00:00Z',
    updated_at: '2026-07-28T00:00:00Z',
    ...overrides,
  }
}

function renderPage() {
  const router = createMemoryRouter(
    [
      { path: '/sprints/:id/exploratory-runs/:runId', element: <ExploratoryRunDetailPage /> },
      { path: '*', element: <div>elsewhere</div> },
    ],
    { initialEntries: ['/sprints/1/exploratory-runs/5'] },
  )
  return render(<RouterProvider router={router} />)
}

describe('ExploratoryRunDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchRun.mockResolvedValue(makeRun())
  })

  it('renders the requirement, summary, and finding counts', async () => {
    renderPage()

    expect(await screen.findByText('Export reports')).toBeInTheDocument()
    expect(screen.getByText('Export is broadly sound.')).toBeInTheDocument()
    expect(screen.getByText(/1 bug/)).toBeInTheDocument()
    expect(screen.getByText(/1 issue/)).toBeInTheDocument()
    expect(screen.getByText(/1 high severity/)).toBeInTheDocument()
  })

  it('lists sessions with their areas and action counts', async () => {
    renderPage()

    expect(await screen.findByText('Explore export edge data')).toBeInTheDocument()
    expect(screen.getByText('Data')).toBeInTheDocument()
    expect(screen.getByText('18 actions')).toBeInTheDocument()
    expect(screen.getByText('2 findings')).toBeInTheDocument()
  })

  it('offers Generate summary when a completed run has none', async () => {
    mockFetchRun.mockResolvedValue(makeRun({ summary: null }))
    mockSummarize.mockResolvedValue(makeRun({ summary: 'Freshly written.' }))
    renderPage()

    const button = await screen.findByRole('button', { name: 'Generate summary' })
    expect(screen.getByText(/No summary was generated/)).toBeInTheDocument()

    fireEvent.click(button)

    await waitFor(() => expect(mockSummarize).toHaveBeenCalledWith(5))
    expect(await screen.findByText('Freshly written.')).toBeInTheDocument()
  })

  it('hides Generate summary when a summary already exists', async () => {
    renderPage()

    await screen.findByText('Export is broadly sound.')
    expect(screen.queryByRole('button', { name: 'Generate summary' })).not.toBeInTheDocument()
  })

  it('surfaces a summary retry failure without losing the sessions', async () => {
    mockFetchRun.mockResolvedValue(makeRun({ summary: null }))
    mockSummarize.mockRejectedValue(new Error('provider down'))
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Generate summary' }))

    expect(await screen.findByText('provider down')).toBeInTheDocument()
    expect(screen.getByText('Explore export edge data')).toBeInTheDocument()
  })

  it('offers Restart only for a failed run', async () => {
    mockFetchRun.mockResolvedValue(makeRun({ status: 'failed', error: 'worker died' }))
    mockRestart.mockResolvedValue(makeRun({ status: 'pending' }))
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Restart run' }))

    await waitFor(() => expect(mockRestart).toHaveBeenCalledWith(5))
  })

  it('does not offer Restart for a completed run', async () => {
    renderPage()

    await screen.findByText('Export reports')
    expect(screen.queryByRole('button', { name: 'Restart run' })).not.toBeInTheDocument()
  })

  it('says the summary is pending while the run is still going', async () => {
    mockFetchRun.mockResolvedValue(makeRun({ status: 'running', summary: null }))
    renderPage()

    expect(await screen.findByText('Available once the run finishes.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Generate summary' })).not.toBeInTheDocument()
  })

  it('surfaces a load error', async () => {
    mockFetchRun.mockRejectedValue(new Error('run vanished'))
    renderPage()

    expect(await screen.findByText('run vanished')).toBeInTheDocument()
  })

  describe('finding export', () => {
    it('shows nothing when the run has no bug findings', async () => {
      mockFetchRun.mockResolvedValue(makeRun())
      renderPage()

      await screen.findByText('Export reports')
      expect(screen.queryByRole('button', { name: /File \d+ bug/ })).not.toBeInTheDocument()
    })

    it('states both totals and lists the tickets', async () => {
      mockFetchRun.mockResolvedValue(
        makeRun({
          exported_finding_count: 4,
          exported_issue_count: 1,
          export_groups: [{ issue_key: '7', issue_url: 'https://gh/7', finding_count: 4 }],
        }),
      )
      renderPage()

      expect(await screen.findByText('4 bugs filed as 1 issue')).toBeInTheDocument()
      expect(screen.getByRole('link', { name: /7/ })).toHaveAttribute('href', 'https://gh/7')
    })

    it('offers to file the bugs of a run that never filed', async () => {
      mockFetchRun.mockResolvedValue(makeRun({ unexported_finding_count: 2 }))
      renderPage()

      expect(await screen.findByRole('button', { name: 'File 2 bugs' })).toBeInTheDocument()
    })

    it('files on click and adopts the refreshed run', async () => {
      mockFetchRun.mockResolvedValue(makeRun({ unexported_finding_count: 2 }))
      mockExport.mockResolvedValue(
        makeRun({
          exported_finding_count: 2,
          exported_issue_count: 1,
          export_groups: [{ issue_key: '9', issue_url: 'https://gh/9', finding_count: 2 }],
        }),
      )
      renderPage()

      fireEvent.click(await screen.findByRole('button', { name: 'File 2 bugs' }))

      expect(await screen.findByText('2 bugs filed as 1 issue')).toBeInTheDocument()
      expect(mockExport).toHaveBeenCalledWith(5)
    })
  })

  describe('polling', () => {
    afterEach(() => {
      vi.useRealTimers()
    })

    it('keeps polling a completed run whose findings have not been filed yet', async () => {
      // The worker commits COMPLETED before it files. Stopping the moment
      // the status reads terminal left the page on "not yet filed" until a
      // manual reload — the bug this window exists to close.
      vi.useFakeTimers()
      mockFetchRun.mockResolvedValue(
        makeRun({ export_findings: true, unexported_finding_count: 2 }),
      )
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(mockFetchRun).toHaveBeenCalledTimes(1)

      // Still filing: the page keeps asking.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500)
      })
      expect(mockFetchRun).toHaveBeenCalledTimes(2)

      // The tickets land — and the page stops asking.
      mockFetchRun.mockResolvedValue(
        makeRun({
          export_findings: true,
          exported_finding_count: 2,
          exported_issue_count: 1,
          export_groups: [{ issue_key: '9', issue_url: 'https://gh/9', finding_count: 2 }],
        }),
      )
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500)
      })
      expect(mockFetchRun).toHaveBeenCalledTimes(3)
      // Read synchronously: `findByText` would wait on a clock this test
      // controls, and nothing is left to advance it.
      expect(screen.getByText('2 bugs filed as 1 issue')).toBeInTheDocument()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(10000)
      })
      expect(mockFetchRun).toHaveBeenCalledTimes(3)
    })

    it('gives up waiting for an export that never arrives', async () => {
      // Bounded, so a completed run whose export silently no-opped cannot
      // poll for as long as the tab stays open.
      vi.useFakeTimers()
      mockFetchRun.mockResolvedValue(
        makeRun({ export_findings: true, unexported_finding_count: 2 }),
      )
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500 * 60)
      })

      // 1 initial + 48 grace ticks, and nothing after.
      expect(mockFetchRun).toHaveBeenCalledTimes(49)
    })

    it('does not poll a failed run holding unfiled bugs', async () => {
      // Nothing is filing: a failed run never reaches the export call, so
      // its unfiled bugs are a standing state, not a pending one.
      vi.useFakeTimers()
      mockFetchRun.mockResolvedValue(
        makeRun({ status: 'failed', export_findings: true, unexported_finding_count: 2 }),
      )
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10000)
      })
      expect(mockFetchRun).toHaveBeenCalledTimes(1)
    })

    it('does not poll a completed run whose filing already failed', async () => {
      // An error is an answer: the run page offers Retry rather than
      // waiting for something that already came back.
      vi.useFakeTimers()
      mockFetchRun.mockResolvedValue(
        makeRun({ export_findings: true, unexported_finding_count: 2, export_error_count: 2 }),
      )
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10000)
      })
      expect(mockFetchRun).toHaveBeenCalledTimes(1)
    })
  })
})
