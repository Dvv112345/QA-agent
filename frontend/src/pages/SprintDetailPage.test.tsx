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
    uploadPrd: vi.fn(),
    confirmAllRequirements: vi.fn(),
  }
})

const mockNavigate = vi.fn()

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  }
})

import {
  confirmAllRequirements,
  fetchRequirements,
  fetchSprint,
  finishSprint,
  submitRequirements,
  uploadPrd,
} from '../services/api'

const mockFetchSprint = fetchSprint as ReturnType<typeof vi.fn>
const mockFinishSprint = finishSprint as ReturnType<typeof vi.fn>
const mockFetchRequirements = fetchRequirements as ReturnType<typeof vi.fn>
const mockSubmitRequirements = submitRequirements as ReturnType<typeof vi.fn>
const mockUploadPrd = uploadPrd as ReturnType<typeof vi.fn>
const mockConfirmAllRequirements = confirmAllRequirements as ReturnType<typeof vi.fn>

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
  requirements_complete: false,
  has_test_environment_submission: false,
  environment_confirmed: false,
  has_test_plans: false,
  test_plans_complete: false,
  has_test_runs: false,
  has_exploratory_runs: false,
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
    from_prd: false,
    status: 'ready',
    clarifying_question: null,
    revision_count: 0,
    clarification_cap_reached: false,
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

    it('shows the requirement form and PRD upload together on active sprints', async () => {
      mockFetchSprint.mockResolvedValue(fakeSprint)
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Sprint 1')).toBeInTheDocument()
      })
      expect(screen.getByText('Add Requirements')).toBeInTheDocument()
      expect(screen.getByText('Upload a PRD')).toBeInTheDocument()
    })

    it('hides the forms and card actions on finished sprints', async () => {
      mockFetchSprint.mockResolvedValue({ ...fakeSprint, active: false })
      mockFetchRequirements.mockResolvedValue([makeRequirement({ status: 'ready' })])
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Login')).toBeInTheDocument()
      })
      expect(screen.queryByText('Add Requirements')).not.toBeInTheDocument()
      expect(screen.queryByText('Upload a PRD')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
    })

    it('replaces old PRD rows but keeps manual ones after an upload', async () => {
      mockFetchSprint.mockResolvedValue(fakeSprint)
      mockFetchRequirements.mockResolvedValue([
        makeRequirement({ id: 1, name: 'Manual', from_prd: false }),
        makeRequirement({ id: 2, name: 'Old PRD', from_prd: true }),
      ])
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Old PRD')).toBeInTheDocument()
      })

      mockUploadPrd.mockResolvedValue([
        makeRequirement({ id: 3, name: 'New PRD', from_prd: true, status: 'pending' }),
      ])
      const file = new File(['# PRD'], 'prd.md', { type: 'text/markdown' })
      fireEvent.change(screen.getByLabelText('PRD file'), { target: { files: [file] } })
      fireEvent.click(screen.getByRole('button', { name: 'Upload PRD' }))
      // an earlier PRD upload exists — the form asks before replacing
      fireEvent.click(await screen.findByRole('button', { name: 'Replace requirements' }))

      await waitFor(() => {
        expect(screen.getByText('New PRD')).toBeInTheDocument()
      })
      expect(screen.getByText('Manual')).toBeInTheDocument()
      expect(screen.queryByText('Old PRD')).not.toBeInTheDocument()
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

  describe('confirm all requirements', () => {
    it('is absent when the sprint has no requirements', async () => {
      mockFetchSprint.mockResolvedValue(fakeSprint)
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Sprint 1')).toBeInTheDocument()
      })
      expect(screen.queryByRole('button', { name: /confirm all/i })).not.toBeInTheDocument()
    })

    it('is absent on finished sprints', async () => {
      mockFetchSprint.mockResolvedValue({ ...fakeSprint, active: false })
      mockFetchRequirements.mockResolvedValue([makeRequirement({ status: 'ready' })])
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Login')).toBeInTheDocument()
      })
      expect(screen.queryByRole('button', { name: /confirm all/i })).not.toBeInTheDocument()
    })

    it('is disabled while any requirement is still pending or analyzing', async () => {
      mockFetchSprint.mockResolvedValue(fakeSprint)
      mockFetchRequirements.mockResolvedValue([
        makeRequirement({ id: 1, status: 'ready' }),
        makeRequirement({ id: 2, name: 'Logout', status: 'analyzing' }),
      ])
      renderPage()

      const button = await screen.findByRole('button', { name: /confirm all/i })
      expect(button).toBeDisabled()
      expect(screen.getByText('Waiting for analysis to finish…')).toBeInTheDocument()
    })

    it('is disabled once settled but nothing is eligible', async () => {
      mockFetchSprint.mockResolvedValue(fakeSprint)
      mockFetchRequirements.mockResolvedValue([
        makeRequirement({ id: 1, status: 'confirmed' }),
        makeRequirement({ id: 2, name: 'Logout', status: 'failed' }),
      ])
      renderPage()

      const button = await screen.findByRole('button', { name: 'Confirm all (0)' })
      expect(button).toBeDisabled()
    })

    it('confirms all eligible requirements and replaces the list', async () => {
      vi.spyOn(window, 'confirm').mockReturnValue(true)
      mockFetchSprint.mockResolvedValue(fakeSprint)
      mockFetchRequirements.mockResolvedValue([
        makeRequirement({ id: 1, status: 'ready' }),
        makeRequirement({ id: 2, name: 'Logout', status: 'needs_clarification' }),
      ])
      renderPage()

      const button = await screen.findByRole('button', { name: 'Confirm all (2)' })
      expect(button).not.toBeDisabled()

      mockConfirmAllRequirements.mockResolvedValue([
        makeRequirement({ id: 1, status: 'confirmed' }),
        makeRequirement({ id: 2, name: 'Logout', status: 'confirmed' }),
      ])
      fireEvent.click(button)

      await waitFor(() => {
        expect(mockConfirmAllRequirements).toHaveBeenCalledWith(1)
      })
      expect(await screen.findByRole('button', { name: 'Confirm all (0)' })).toBeDisabled()
    })

    it('does not call the API when the confirmation dialog is declined', async () => {
      vi.spyOn(window, 'confirm').mockReturnValue(false)
      mockFetchSprint.mockResolvedValue(fakeSprint)
      mockFetchRequirements.mockResolvedValue([makeRequirement({ id: 1, status: 'ready' })])
      renderPage()

      fireEvent.click(await screen.findByRole('button', { name: 'Confirm all (1)' }))

      expect(mockConfirmAllRequirements).not.toHaveBeenCalled()
    })

    it('surfaces errors from a rejected confirm-all call', async () => {
      vi.spyOn(window, 'confirm').mockReturnValue(true)
      mockFetchSprint.mockResolvedValue(fakeSprint)
      mockFetchRequirements.mockResolvedValue([makeRequirement({ id: 1, status: 'ready' })])
      mockConfirmAllRequirements.mockRejectedValue(new Error('Confirm-all failed'))
      renderPage()

      fireEvent.click(await screen.findByRole('button', { name: 'Confirm all (1)' }))

      expect(await screen.findByText('Confirm-all failed')).toBeInTheDocument()
    })
  })

  describe('continue to test environment', () => {
    it('hides Continue when there are no requirements', async () => {
      mockFetchSprint.mockResolvedValue(fakeSprint)
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Sprint 1')).toBeInTheDocument()
      })
      expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
    })

    it('hides Continue on finished sprints', async () => {
      mockFetchSprint.mockResolvedValue({ ...fakeSprint, active: false })
      mockFetchRequirements.mockResolvedValue([makeRequirement({ status: 'confirmed' })])
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Login')).toBeInTheDocument()
      })
      expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
    })

    it('shows inline error and does not navigate when requirements are incomplete', async () => {
      mockFetchSprint.mockResolvedValue({ ...fakeSprint, requirements_complete: false })
      mockFetchRequirements.mockResolvedValue([makeRequirement({ status: 'ready' })])
      renderPage()

      fireEvent.click(await screen.findByRole('button', { name: 'Continue' }))

      expect(
        await screen.findByText('Confirm or delete the remaining requirements before continuing.'),
      ).toBeInTheDocument()
      expect(mockNavigate).not.toHaveBeenCalled()
    })

    it('re-fetches the sprint and navigates when requirements are complete', async () => {
      // The mount-time flag is stale on purpose — the click handler must
      // trust the re-fetched value.
      mockFetchSprint
        .mockResolvedValueOnce({ ...fakeSprint, requirements_complete: false })
        .mockResolvedValue({ ...fakeSprint, requirements_complete: true })
      mockFetchRequirements.mockResolvedValue([makeRequirement({ status: 'confirmed' })])
      renderPage()

      fireEvent.click(await screen.findByRole('button', { name: 'Continue' }))

      await waitFor(() => {
        expect(mockNavigate).toHaveBeenCalledWith('/sprints/1/test-environment')
      })
    })
  })

  describe('requirement lock', () => {
    it('hides the form and Remove buttons when requirements are locked', async () => {
      mockFetchSprint.mockResolvedValue({
        ...fakeSprint,
        requirements_complete: true,
        environment_confirmed: true,
      })
      mockFetchRequirements.mockResolvedValue([makeRequirement({ status: 'confirmed' })])
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Login')).toBeInTheDocument()
      })
      expect(screen.queryByText('Add Requirements')).not.toBeInTheDocument()
      expect(screen.queryByText('Upload a PRD')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
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
