import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import CicdConfigModal from './CicdConfigModal'
import type { CicdConfig, RepoResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return { ...actual, saveCicdConfig: vi.fn(), deleteCicdConfig: vi.fn() }
})

import { deleteCicdConfig, saveCicdConfig } from '../services/api'

const mockSave = saveCicdConfig as ReturnType<typeof vi.fn>
const mockDelete = deleteCicdConfig as ReturnType<typeof vi.fn>

const repo: RepoResponse = {
  id: 1,
  github_link: 'https://github.com/owner/repo',
  name: 'owner/repo',
  description: null,
  active: true,
  has_access_token: true,
  created_at: '2026-08-18T10:00:00Z',
}

function makeConfig(overrides: Partial<CicdConfig> = {}): CicdConfig {
  return {
    id: 1,
    sprint_id: 1,
    provider: 'github_actions',
    ci_environment_hint: null,
    verified_at: '2026-08-18T10:00:00Z',
    created_at: '2026-08-18T10:00:00Z',
    updated_at: '2026-08-18T10:00:00Z',
    ...overrides,
  }
}

function renderModal(config: CicdConfig | null = null, onSaved = vi.fn(), onClose = vi.fn()) {
  render(
    <CicdConfigModal
      sprintId={1}
      config={config}
      repo={repo}
      onSaved={onSaved}
      onClose={onClose}
    />,
  )
  return { onSaved, onClose }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('CicdConfigModal', () => {
  it('names the repository the export will target', () => {
    renderModal()

    expect(screen.getByText('owner/repo')).toBeInTheDocument()
  })

  it('has no repository field — the destination is derived server-side', () => {
    renderModal()

    expect(screen.queryByPlaceholderText('owner/repo')).not.toBeInTheDocument()
  })

  it('saves the provider, token and hint', async () => {
    mockSave.mockResolvedValue(makeConfig())
    const { onSaved, onClose } = renderModal()

    fireEvent.change(screen.getByLabelText(/Access token/), { target: { value: 'ghp_write' } })
    fireEvent.change(screen.getByLabelText(/CI environment notes/), {
      target: { value: 'self-hosted runner' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith(1, {
        provider: 'github_actions',
        access_token: 'ghp_write',
        ci_environment_hint: 'self-hosted runner',
      }),
    )
    expect(onSaved).toHaveBeenCalled()
    expect(onClose).toHaveBeenCalled()
  })

  it('surfaces the push-permission refusal verbatim', async () => {
    mockSave.mockRejectedValue(
      new Error(
        'This token can read the repository but cannot push to it. ' +
          'A token with write access to contents and pull requests is required.',
      ),
    )
    renderModal()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/cannot push to it/)
  })

  it('offers to keep the stored token on an edit', () => {
    renderModal(makeConfig())

    expect(screen.getByPlaceholderText(/keep the current token/)).toBeInTheDocument()
  })

  it("offers the repository's own token on a first connect", () => {
    renderModal(null)

    expect(screen.getByPlaceholderText(/use the repository's access token/)).toBeInTheDocument()
  })

  it('keeps the stored token across a provider switch', async () => {
    mockSave.mockResolvedValue(makeConfig({ provider: 'jenkins' }))
    renderModal(makeConfig())

    fireEvent.click(screen.getByLabelText('Jenkins'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    // Blank token on a switch is legitimate here, unlike the issue tracker:
    // both providers ship as a GitHub pull request.
    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith(1, {
        provider: 'jenkins',
        access_token: '',
        ci_environment_hint: '',
      }),
    )
  })

  it('offers Disconnect only for an existing connection', () => {
    const { rerender } = render(
      <CicdConfigModal
        sprintId={1}
        config={null}
        repo={repo}
        onSaved={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument()

    rerender(
      <CicdConfigModal
        sprintId={1}
        config={makeConfig()}
        repo={repo}
        onSaved={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument()
  })

  it('disconnects and reports null upward', async () => {
    mockDelete.mockResolvedValue(undefined)
    const { onSaved } = renderModal(makeConfig())

    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(null))
  })
})
