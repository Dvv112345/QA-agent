import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import SprintDetailPage from './SprintDetailPage'
import type { RequirementResponse, SprintResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchSprint: vi.fn(),
    finishSprint: vi.fn(),
    fetchRequirements: vi.fn(),
    submitRequirements: vi.fn(),
  }
})

import { fetchRequirements, fetchSprint, finishSprint, submitRequirements } from '../services/api'

const mockFetchSprint = fetchSprint as ReturnType<typeof vi.fn>
const mockFinishSprint = finishSprint as ReturnType<typeof vi.fn>
const mockFetchRequirements = fetchRequirements as ReturnType<typeof vi.fn>
const mockSubmitRequirements = submitRequirements as ReturnType<typeof vi.fn>

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

function makeRequirement(overrides: Partial<RequirementResponse> = {}): RequirementResponse {
  return {
    id: 1,
    sprint_id: 1,
    name: 'Login',
    description: 'Users can log in.',
    original_description: 'Users can log in.',
    status: 'ready',
    clarifying_question: null,
    revision_count: 0,
    error: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('SprintDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchRequirements.mockResolvedValue([])
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

  describe('requirements section', () => {
    it('renders fetched requirement cards with a summary', async () => {
      mockFetchSprint.mockResolvedValue(fakeSprint)
      mockFetchRequirements.mockResolvedValue([
        makeRequirement({ id: 1, name: 'Login', status: 'ready' }),
        makeRequirement({ id: 2, name: 'Logout', status: 'pending' }),
      ])
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Login')).toBeInTheDocument()
      })
      expect(screen.getByText('Logout')).toBeInTheDocument()
      expect(screen.getByText('1 of 2 analyzed')).toBeInTheDocument()
    })

    it('shows the requirement form on active sprints', async () => {
      mockFetchSprint.mockResolvedValue(fakeSprint)
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Sprint 1')).toBeInTheDocument()
      })
      expect(screen.getByText('Add Requirements')).toBeInTheDocument()
    })

    it('hides the form and card actions on finished sprints', async () => {
      mockFetchSprint.mockResolvedValue({ ...fakeSprint, active: false })
      mockFetchRequirements.mockResolvedValue([makeRequirement({ status: 'ready' })])
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Login')).toBeInTheDocument()
      })
      expect(screen.queryByText('Add Requirements')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
    })

    it('appends rows submitted through the form', async () => {
      mockFetchSprint.mockResolvedValue(fakeSprint)
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Add Requirements')).toBeInTheDocument()
      })

      mockSubmitRequirements.mockResolvedValue([
        makeRequirement({ id: 9, name: 'Search', status: 'pending' }),
      ])

      fireEvent.change(screen.getByLabelText('Requirement 1 name'), {
        target: { value: 'Search' },
      })
      fireEvent.change(screen.getByLabelText('Requirement 1 description'), {
        target: { value: 'Users can search.' },
      })
      fireEvent.click(screen.getByRole('button', { name: 'Submit Requirements' }))

      await waitFor(() => {
        expect(screen.getByText('Search')).toBeInTheDocument()
      })
    })
  })

  describe('polling', () => {
    afterEach(() => {
      vi.useRealTimers()
    })

    it('polls while a requirement is pending and stops once all are terminal', async () => {
      vi.useFakeTimers()
      mockFetchSprint.mockResolvedValue(fakeSprint)
      mockFetchRequirements.mockResolvedValue([makeRequirement({ status: 'pending' })])
      renderPage()

      // Flush the initial fetches
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(mockFetchRequirements).toHaveBeenCalledTimes(1)

      // First tick: still pending → keeps polling
      mockFetchRequirements.mockResolvedValue([makeRequirement({ status: 'analyzing' })])
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500)
      })
      expect(mockFetchRequirements).toHaveBeenCalledTimes(2)

      // Second tick: now terminal → polling stops
      mockFetchRequirements.mockResolvedValue([makeRequirement({ status: 'ready' })])
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500)
      })
      expect(mockFetchRequirements).toHaveBeenCalledTimes(3)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(10000)
      })
      expect(mockFetchRequirements).toHaveBeenCalledTimes(3)
    })

    it('does not poll when all requirements are terminal', async () => {
      vi.useFakeTimers()
      mockFetchSprint.mockResolvedValue(fakeSprint)
      mockFetchRequirements.mockResolvedValue([makeRequirement({ status: 'confirmed' })])
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })

      await act(async () => {
        await vi.advanceTimersByTimeAsync(10000)
      })
      expect(mockFetchRequirements).toHaveBeenCalledTimes(1)
    })
  })
})
