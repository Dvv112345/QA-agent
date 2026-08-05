import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithRouter } from '../test/test-utils'
import CreateSprintPage from './CreateSprintPage'
import type { RepoResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchRepos: vi.fn(),
    checkReadmeStatus: vi.fn(),
    createSprint: vi.fn(),
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

import { fetchRepos, checkReadmeStatus, createSprint } from '../services/api'

const mockFetchRepos = fetchRepos as ReturnType<typeof vi.fn>
const mockCheckReadmeStatus = checkReadmeStatus as ReturnType<typeof vi.fn>
const mockCreateSprint = createSprint as ReturnType<typeof vi.fn>

const fakeRepo: RepoResponse = {
  id: 1,
  github_link: 'https://github.com/owner/repo',
  name: 'owner/repo',
  description: null,
  active: true,
  created_at: '2026-01-01T00:00:00Z',
  has_access_token: false,
}

function renderPage() {
  return renderWithRouter(<CreateSprintPage />)
}

describe('CreateSprintPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows "Loading repos" while repos are being fetched', () => {
    mockFetchRepos.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByText(/loading repos/i)).toBeInTheDocument()
  })

  it('shows error when fetchRepos fails', async () => {
    mockFetchRepos.mockRejectedValue(new Error('API error'))
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('API error')).toBeInTheDocument()
    })
  })

  it('populates repo dropdown on load', async () => {
    mockFetchRepos.mockResolvedValue([fakeRepo])
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('owner/repo')).toBeInTheDocument()
    })
  })

  it('shows README found when checkReadmeStatus returns true', async () => {
    mockFetchRepos.mockResolvedValue([fakeRepo])
    mockCheckReadmeStatus.mockResolvedValue({ has_readme: true })
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('owner/repo')).toBeInTheDocument()
    })

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '1' } })

    await waitFor(() => {
      expect(screen.getByText(/readme found/i)).toBeInTheDocument()
    })
  })

  it('shows README required when checkReadmeStatus returns false', async () => {
    mockFetchRepos.mockResolvedValue([fakeRepo])
    mockCheckReadmeStatus.mockResolvedValue({ has_readme: false })
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('owner/repo')).toBeInTheDocument()
    })

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '1' } })

    await waitFor(() => {
      expect(screen.getByText(/no readme/i)).toBeInTheDocument()
    })
    // File input should be required
    const fileInput = screen.getByLabelText(/required/) as HTMLInputElement
    expect(fileInput.required).toBe(true)
  })

  it('calls createSprint on form submission', async () => {
    mockFetchRepos.mockResolvedValue([fakeRepo])
    mockCheckReadmeStatus.mockResolvedValue({ has_readme: true })
    mockCreateSprint.mockResolvedValue({ id: 42 } as never)
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('owner/repo')).toBeInTheDocument()
    })

    // Select repo
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '1' } })

    await waitFor(() => {
      expect(screen.getByText(/readme found/i)).toBeInTheDocument()
    })

    // Fill name
    fireEvent.change(screen.getByPlaceholderText('e.g. Sprint 1'), {
      target: { value: 'My Sprint' },
    })

    // Submit
    fireEvent.click(screen.getByRole('button', { name: 'Create Sprint' }))

    await waitFor(() => {
      expect(mockCreateSprint).toHaveBeenCalledWith('My Sprint', 1, undefined)
    })
  })

  it('navigates to the sprint detail page after creation', async () => {
    mockFetchRepos.mockResolvedValue([fakeRepo])
    mockCheckReadmeStatus.mockResolvedValue({ has_readme: true })
    mockCreateSprint.mockResolvedValue({ id: 42 } as never)
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('owner/repo')).toBeInTheDocument()
    })

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '1' } })

    await waitFor(() => {
      expect(screen.getByText(/readme found/i)).toBeInTheDocument()
    })

    fireEvent.change(screen.getByPlaceholderText('e.g. Sprint 1'), {
      target: { value: 'My Sprint' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create Sprint' }))

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith('/sprints/42')
    })
  })

  it('submit button is disabled when README is required but not provided', async () => {
    mockFetchRepos.mockResolvedValue([fakeRepo])
    mockCheckReadmeStatus.mockResolvedValue({ has_readme: false })
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('owner/repo')).toBeInTheDocument()
    })

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '1' } })

    await waitFor(() => {
      expect(screen.getByText(/no readme/i)).toBeInTheDocument()
    })

    // Button should be disabled because no README file is uploaded
    expect(screen.getByRole('button', { name: 'Create Sprint' })).toBeDisabled()
  })
})
