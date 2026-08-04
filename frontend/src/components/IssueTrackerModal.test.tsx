import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import IssueTrackerModal from './IssueTrackerModal'
import type { IssueTrackerConfig } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    saveIssueTracker: vi.fn(),
    deleteIssueTracker: vi.fn(),
  }
})

import { deleteIssueTracker, saveIssueTracker } from '../services/api'

const mockSave = saveIssueTracker as ReturnType<typeof vi.fn>
const mockDelete = deleteIssueTracker as ReturnType<typeof vi.fn>

function makeConfig(overrides: Partial<IssueTrackerConfig> = {}): IssueTrackerConfig {
  return {
    id: 1,
    sprint_id: 1,
    provider: 'jira',
    target: 'QA',
    target_label: 'Jira · QA',
    base_url: 'https://acme.atlassian.net',
    account_email: 'qa@acme.test',
    issue_type: 'Bug',
    verified_at: '2026-01-01T00:00:00Z',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderModal(config: IssueTrackerConfig | null = null) {
  const onSaved = vi.fn()
  const onClose = vi.fn()
  render(<IssueTrackerModal sprintId={1} config={config} onSaved={onSaved} onClose={onClose} />)
  return { onSaved, onClose }
}

describe('IssueTrackerModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockSave.mockResolvedValue(makeConfig())
    mockDelete.mockResolvedValue(undefined)
  })

  it('shows the Jira field set by default', () => {
    renderModal()

    expect(screen.getByLabelText(/Jira site URL/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Account email/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Project key/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Issue type/)).toBeInTheDocument()
  })

  it('switching provider swaps the field set', () => {
    renderModal()

    fireEvent.click(screen.getByLabelText('GitHub Issues'))

    expect(screen.getByLabelText(/Repository/)).toBeInTheDocument()
    expect(screen.queryByLabelText(/Jira site URL/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/Issue type/)).not.toBeInTheDocument()
  })

  it('sends only the fields the chosen provider uses', async () => {
    renderModal()
    fireEvent.click(screen.getByLabelText('GitHub Issues'))
    fireEvent.change(screen.getByLabelText(/Repository/), { target: { value: 'acme/shop' } })
    fireEvent.change(screen.getByLabelText(/API token/), { target: { value: 'tok' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(mockSave).toHaveBeenCalled())
    expect(mockSave).toHaveBeenCalledWith(1, {
      provider: 'github',
      target: 'acme/shop',
      base_url: null,
      account_email: null,
      issue_type: null,
      api_token: 'tok',
    })
  })

  it('opens pre-filled from an existing config, with the token blank', () => {
    renderModal(makeConfig())

    expect(screen.getByLabelText(/Jira site URL/)).toHaveValue('https://acme.atlassian.net')
    expect(screen.getByLabelText(/Project key/)).toHaveValue('QA')
    expect(screen.getByLabelText(/API token/)).toHaveValue('')
  })

  it('offers to keep the stored token on a same-provider edit', () => {
    renderModal(makeConfig())

    expect(screen.getByLabelText(/API token/)).toHaveAttribute(
      'placeholder',
      'Leave blank to keep the current token',
    )
  })

  it('demands a token the moment the provider is switched', () => {
    // The backend rejects a blank token on a switch — a Jira token is
    // meaningless to GitHub — so the placeholder has to say so before
    // the user presses Save.
    renderModal(makeConfig())

    fireEvent.click(screen.getByLabelText('GitHub Issues'))

    expect(screen.getByLabelText(/API token/)).toHaveAttribute(
      'placeholder',
      'Required when changing provider',
    )
  })

  it('clears the target when switching away from the stored provider', () => {
    // "QA" is a Jira project key and means nothing as a GitHub repo.
    renderModal(makeConfig())

    fireEvent.click(screen.getByLabelText('GitHub Issues'))

    expect(screen.getByLabelText(/Repository/)).toHaveValue('')
  })

  it('restores the stored target when switching back', () => {
    renderModal(makeConfig())

    fireEvent.click(screen.getByLabelText('GitHub Issues'))
    fireEvent.click(screen.getByLabelText('Jira'))

    expect(screen.getByLabelText(/Project key/)).toHaveValue('QA')
  })

  it('renders the verification error inline and stays open', async () => {
    mockSave.mockRejectedValue(new Error("Issue type 'Defect' does not exist in project QA."))
    const { onClose } = renderModal()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(
        screen.getByText("Issue type 'Defect' does not exist in project QA."),
      ).toBeInTheDocument()
    })
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('reports the saved config to the parent and closes', async () => {
    const { onSaved, onClose } = renderModal()
    fireEvent.change(screen.getByLabelText(/API token/), { target: { value: 'tok' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(makeConfig()))
    expect(onClose).toHaveBeenCalled()
  })

  it('offers Disconnect only when something is connected', () => {
    renderModal()
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument()
  })

  it('disconnecting reports null to the parent', async () => {
    const { onSaved, onClose } = renderModal(makeConfig())

    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(null))
    expect(mockDelete).toHaveBeenCalledWith(1)
    expect(onClose).toHaveBeenCalled()
  })

  it('surfaces a disconnect failure without closing', async () => {
    mockDelete.mockRejectedValue(new Error('No issue tracker is connected.'))
    const { onClose } = renderModal(makeConfig())

    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))

    await waitFor(() => {
      expect(screen.getByText('No issue tracker is connected.')).toBeInTheDocument()
    })
    expect(onClose).not.toHaveBeenCalled()
  })
})
