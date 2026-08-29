import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithRouter } from '../test/test-utils'
import SprintListPage from './SprintListPage'
import type { SprintResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchSprints: vi.fn(),
    finishSprint: vi.fn(),
  }
})

import { fetchSprints, finishSprint } from '../services/api'

const mockFetchSprints = fetchSprints as ReturnType<typeof vi.fn>
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
    description: null,
    active: true,
    created_at: '2026-01-01T00:00:00Z',
    has_access_token: false,
  },
  requirements_complete: false,
  has_test_environment_submission: false,
  environment_confirmed: false,
  has_test_plans: false,
  test_plans_missing: false,
  test_plans_complete: false,
  has_test_runs: false,
  has_exploratory_runs: false,
  has_nonfunctional_runs: false,
}

function renderPage() {
  return renderWithRouter(<SprintListPage />)
}

describe('SprintListPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows loading state initially', () => {
    mockFetchSprints.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByText(/loading sprints/i)).toBeInTheDocument()
  })

  it('shows error when fetch fails', async () => {
    mockFetchSprints.mockRejectedValue(new Error('API error'))
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('API error')).toBeInTheDocument()
    })
  })

  it('shows empty state when no sprints', async () => {
    mockFetchSprints.mockResolvedValue([])
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/no sprints yet/i)).toBeInTheDocument()
    })
  })

  it('renders sprint cards', async () => {
    mockFetchSprints.mockResolvedValue([fakeSprint])
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Sprint 1')).toBeInTheDocument()
    })
    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(screen.getByText('owner/repo')).toBeInTheDocument()
  })

  it('calls finishSprint and refetches on button click', async () => {
    const finished = { ...fakeSprint, active: false }
    mockFetchSprints.mockResolvedValue([fakeSprint])
    mockFinishSprint.mockResolvedValue(finished)
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Sprint 1')).toBeInTheDocument()
    })

    // After finish, refetch returns the finished sprint
    mockFetchSprints.mockResolvedValue([finished])

    fireEvent.click(screen.getByRole('button', { name: 'Finish Sprint' }))

    // The dialog gates the call — nothing is sent until it is confirmed.
    expect(mockFinishSprint).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: 'Finish sprint' }))

    await waitFor(() => {
      expect(mockFinishSprint).toHaveBeenCalledWith(1)
    })
  })

  it('has navigation links', async () => {
    mockFetchSprints.mockResolvedValue([])
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/no sprints yet/i)).toBeInTheDocument()
    })
    expect(screen.getByRole('link', { name: 'Create New Sprint' })).toHaveAttribute(
      'href',
      '/sprints/new',
    )
    expect(screen.getByRole('link', { name: 'Manage Repos' })).toHaveAttribute('href', '/repos')
  })

  describe('card landing link', () => {
    it.each([
      [true, true, '/sprints/1/test-environment'],
      [true, false, '/sprints/1'],
      [false, true, '/sprints/1'],
      [false, false, '/sprints/1'],
    ])('active=%s has_submission=%s links to %s', async (active, hasSubmission, expectedHref) => {
      mockFetchSprints.mockResolvedValue([
        { ...fakeSprint, active, has_test_environment_submission: hasSubmission },
      ])
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Sprint 1')).toBeInTheDocument()
      })
      expect(screen.getByRole('link', { name: /sprint 1/i })).toHaveAttribute('href', expectedHref)
    })

    it('prefers the test-plans page when the sprint has plans', async () => {
      mockFetchSprints.mockResolvedValue([
        {
          ...fakeSprint,
          active: true,
          has_test_environment_submission: true,
          has_test_plans: true,
        },
      ])
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Sprint 1')).toBeInTheDocument()
      })
      expect(screen.getByRole('link', { name: /sprint 1/i })).toHaveAttribute(
        'href',
        '/sprints/1/test-plans',
      )
    })

    it('ignores has_test_plans on finished sprints', async () => {
      mockFetchSprints.mockResolvedValue([
        {
          ...fakeSprint,
          active: false,
          has_test_environment_submission: true,
          has_test_plans: true,
        },
      ])
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Sprint 1')).toBeInTheDocument()
      })
      expect(screen.getByRole('link', { name: /sprint 1/i })).toHaveAttribute('href', '/sprints/1')
    })

    it('prefers the test-runs page when the sprint has runs', async () => {
      mockFetchSprints.mockResolvedValue([
        {
          ...fakeSprint,
          active: true,
          has_test_environment_submission: true,
          has_test_plans: true,
          has_test_runs: true,
          has_exploratory_runs: false,
          has_nonfunctional_runs: false,
        },
      ])
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Sprint 1')).toBeInTheDocument()
      })
      expect(screen.getByRole('link', { name: /sprint 1/i })).toHaveAttribute(
        'href',
        '/sprints/1/test-runs',
      )
    })

    it('ignores has_test_runs on finished sprints', async () => {
      mockFetchSprints.mockResolvedValue([
        {
          ...fakeSprint,
          active: false,
          has_test_environment_submission: true,
          has_test_plans: true,
          has_test_runs: true,
          has_exploratory_runs: false,
          has_nonfunctional_runs: false,
        },
      ])
      renderPage()

      await waitFor(() => {
        expect(screen.getByText('Sprint 1')).toBeInTheDocument()
      })
      expect(screen.getByRole('link', { name: /sprint 1/i })).toHaveAttribute('href', '/sprints/1')
    })
  })
})
