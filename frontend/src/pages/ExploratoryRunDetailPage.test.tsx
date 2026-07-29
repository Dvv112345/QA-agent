import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  }
})

import {
  fetchExploratoryRun,
  restartExploratoryRun,
  summarizeExploratoryRun,
} from '../services/api'

const mockFetchRun = fetchExploratoryRun as ReturnType<typeof vi.fn>
const mockRestart = restartExploratoryRun as ReturnType<typeof vi.fn>
const mockSummarize = summarizeExploratoryRun as ReturnType<typeof vi.fn>

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
})
