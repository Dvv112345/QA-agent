import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import ExploratorySessionPage from './ExploratorySessionPage'
import type { ExploratorySessionResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return { ...actual, fetchExploratorySession: vi.fn() }
})

import { fetchExploratorySession } from '../services/api'

const mockFetchSession = fetchExploratorySession as ReturnType<typeof vi.fn>

function makeSession(
  overrides: Partial<ExploratorySessionResponse> = {},
): ExploratorySessionResponse {
  return {
    id: 21,
    exploratory_run_id: 5,
    position: 0,
    charter: 'Explore export with unusual data',
    sfdipot_areas: ['Data', 'Function'],
    status: 'completed',
    actions_used: 18,
    session_notes: 'Exported with zero rows and got an empty file.',
    action_log: 'snapshot() -> page\nclick(ref=e3) -> clicked',
    stop_reason: 'charter_complete',
    error: null,
    findings: [
      {
        id: 7,
        position: 0,
        finding_type: 'bug',
        severity: 'high',
        title: 'Empty export omits the header row',
        steps_to_reproduce: 'Open reports\nClick Export',
        expected: 'A header row',
        actual: 'Zero bytes',
        has_screenshot: false,
        created_at: '2026-07-28T00:00:00Z',
      },
    ],
    updated_at: '2026-07-28T00:00:00Z',
    ...overrides,
  }
}

function renderPage() {
  const router = createMemoryRouter(
    [
      {
        path: '/sprints/:id/exploratory-sessions/:sessionId',
        element: <ExploratorySessionPage />,
      },
      { path: '*', element: <div>elsewhere</div> },
    ],
    { initialEntries: ['/sprints/1/exploratory-sessions/21'] },
  )
  return render(<RouterProvider router={router} />)
}

describe('ExploratorySessionPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchSession.mockResolvedValue(makeSession())
  })

  it('renders the SBTM session sheet fields', async () => {
    renderPage()

    expect(await screen.findByText('Explore export with unusual data')).toBeInTheDocument()
    expect(screen.getByText('Data, Function')).toBeInTheDocument()
    expect(screen.getByText('18')).toBeInTheDocument()
    expect(screen.getByText('Charter explored')).toBeInTheDocument()
    expect(screen.getByText('Exported with zero rows and got an empty file.')).toBeInTheDocument()
  })

  it('renders findings', async () => {
    renderPage()

    expect(await screen.findByText('Empty export omits the header row')).toBeInTheDocument()
    expect(screen.getByText('Findings (1)')).toBeInTheDocument()
  })

  it('says so when a session recorded nothing', async () => {
    mockFetchSession.mockResolvedValue(makeSession({ findings: [] }))
    renderPage()

    expect(await screen.findByText('This session recorded no findings.')).toBeInTheDocument()
  })

  it('puts the action log behind a disclosure', async () => {
    renderPage()

    const summary = await screen.findByText('Action log')
    expect(summary.closest('details')).not.toBeNull()
    expect(screen.getByText(/snapshot\(\) -> page/)).toBeInTheDocument()
  })

  it('labels an action-cap stop reason in plain language', async () => {
    mockFetchSession.mockResolvedValue(makeSession({ stop_reason: 'action_cap' }))
    renderPage()

    expect(await screen.findByText('Time box exhausted')).toBeInTheDocument()
  })

  it('shows a session error when one occurred', async () => {
    mockFetchSession.mockResolvedValue(
      makeSession({ status: 'error', error: 'browser exploded', session_notes: null }),
    )
    renderPage()

    expect(await screen.findByText('browser exploded')).toBeInTheDocument()
    expect(screen.getByText('No notes were recorded.')).toBeInTheDocument()
  })
})
