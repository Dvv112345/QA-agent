import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import SprintDetailPage from './SprintDetailPage'
import type { SprintResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchSprint: vi.fn(),
    finishSprint: vi.fn(),
  }
})

import { fetchSprint, finishSprint } from '../services/api'

const mockFetchSprint = fetchSprint as ReturnType<typeof vi.fn>
const mockFinishSprint = finishSprint as ReturnType<typeof vi.fn>

const fakeSprint: SprintResponse = {
  id: 1,
  name: 'Sprint 1',
  repo_id: 1,
  active: true,
  directory: 'abc123',
  created_at: '2026-01-01T00:00:00Z',
  repo: {
    id: 1,
    github_link: 'https://github.com/owner/repo',
    name: 'owner/repo',
    description: 'A test repo',
    active: true,
    created_at: '2026-01-01T00:00:00Z',
  },
}

/**
 * Render SprintDetailPage with a memory router that provides the ``:id`` param.
 * Needed because ``useParams<{ id: string }>()`` requires a matching route pattern.
 */
function renderPage(sprintId = '1') {
  const router = createMemoryRouter([{ path: '/sprints/:id', element: <SprintDetailPage /> }], {
    initialEntries: [`/sprints/${sprintId}`],
  })
  return render(<RouterProvider router={router} />)
}

describe('SprintDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows loading state initially', () => {
    mockFetchSprint.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByText(/loading sprint/i)).toBeInTheDocument()
  })

  it('shows error when fetch fails', async () => {
    mockFetchSprint.mockRejectedValue(new Error('API error'))
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('API error')).toBeInTheDocument()
    })
  })

  it('shows not found when sprint is null', async () => {
    mockFetchSprint.mockResolvedValue(null)
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Sprint not found.')).toBeInTheDocument()
    })
  })

  it('renders active sprint with repo info and finish button', async () => {
    mockFetchSprint.mockResolvedValue(fakeSprint)
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Sprint 1')).toBeInTheDocument()
    })
    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(screen.getByText('owner/repo')).toBeInTheDocument()
    expect(screen.getByText('A test repo')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Finish Sprint' })).toBeInTheDocument()
  })

  it('hides finish button for finished sprint', async () => {
    mockFetchSprint.mockResolvedValue({ ...fakeSprint, active: false })
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Finished')).toBeInTheDocument()
    })
    expect(screen.queryByRole('button', { name: 'Finish Sprint' })).not.toBeInTheDocument()
  })

  it('calls finishSprint on button click', async () => {
    mockFetchSprint.mockResolvedValue(fakeSprint)
    mockFinishSprint.mockResolvedValue({ ...fakeSprint, active: false })
    renderPage()

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Finish Sprint' })).toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Finish Sprint' }))

    await waitFor(() => {
      expect(mockFinishSprint).toHaveBeenCalledWith(1)
    })
  })

  it('has back link to sprints list', async () => {
    mockFetchSprint.mockResolvedValue(fakeSprint)
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Sprint 1')).toBeInTheDocument()
    })
    expect(screen.getByRole('link', { name: /back to sprints/i })).toHaveAttribute('href', '/')
  })
})
