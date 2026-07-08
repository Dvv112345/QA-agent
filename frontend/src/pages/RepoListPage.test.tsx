import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithRouter } from '../test/test-utils'
import RepoListPage from './RepoListPage'
import type { RepoResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchRepos: vi.fn(),
    deactivateRepo: vi.fn(),
  }
})

import { fetchRepos, deactivateRepo } from '../services/api'

const mockFetchRepos = fetchRepos as ReturnType<typeof vi.fn>
const mockDeactivateRepo = deactivateRepo as ReturnType<typeof vi.fn>

const fakeRepo: RepoResponse = {
  id: 1,
  github_link: 'https://github.com/owner/repo',
  name: 'owner/repo',
  description: 'A test repo',
  active: true,
  created_at: '2026-01-01T00:00:00Z',
}

function renderPage() {
  return renderWithRouter(<RepoListPage />)
}

describe('RepoListPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows loading state initially', () => {
    mockFetchRepos.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByText(/loading repos/i)).toBeInTheDocument()
  })

  it('shows error when fetch fails', async () => {
    mockFetchRepos.mockRejectedValue(new Error('API error'))
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('API error')).toBeInTheDocument()
    })
  })

  it('shows empty state when no repos', async () => {
    mockFetchRepos.mockResolvedValue([])
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/no repos stored yet/i)).toBeInTheDocument()
    })
  })

  it('renders repo cards', async () => {
    mockFetchRepos.mockResolvedValue([fakeRepo])
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('owner/repo')).toBeInTheDocument()
    })
    expect(screen.getByText('A test repo')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: fakeRepo.github_link })).toHaveAttribute(
      'href',
      fakeRepo.github_link,
    )
  })

  it('calls deactivateRepo and refetches on button click', async () => {
    mockFetchRepos.mockResolvedValue([fakeRepo])
    mockDeactivateRepo.mockResolvedValue(undefined)
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('owner/repo')).toBeInTheDocument()
    })

    // After deactivation, refetch returns empty
    mockFetchRepos.mockResolvedValue([])

    fireEvent.click(screen.getByRole('button', { name: 'Deactivate' }))

    await waitFor(() => {
      expect(mockDeactivateRepo).toHaveBeenCalledWith(1)
    })
  })

  it('shows deactivation error', async () => {
    mockFetchRepos.mockResolvedValue([fakeRepo])
    mockDeactivateRepo.mockRejectedValue(new Error('Deactivation failed'))
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('owner/repo')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Deactivate' }))

    await waitFor(() => {
      expect(screen.getByText('Deactivation failed')).toBeInTheDocument()
    })
  })

  it('has back link to sprints', async () => {
    mockFetchRepos.mockResolvedValue([])
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/no repos stored yet/i)).toBeInTheDocument()
    })
    expect(screen.getByRole('link', { name: /back to sprints/i })).toHaveAttribute('href', '/')
  })
})
