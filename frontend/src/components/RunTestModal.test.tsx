import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import RunTestModal from './RunTestModal'
import type { IssueTrackerConfig, TestPlanResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchTestPlans: vi.fn(),
    createTestRun: vi.fn(),
  }
})

import { createTestRun } from '../services/api'

const mockCreateTestRun = createTestRun as ReturnType<typeof vi.fn>

function makePlan(overrides: Partial<TestPlanResponse> = {}): TestPlanResponse {
  return {
    id: 10,
    requirement_id: 100,
    requirement_name: 'Login',
    requirement_description: 'Users can log in.',
    status: 'approved',
    complexity: 'medium',
    summary: 'Covers login.',
    revision_count: 0,
    feedback_cap_reached: false,
    error: null,
    cases: [],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeTracker(overrides: Partial<IssueTrackerConfig> = {}): IssueTrackerConfig {
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

/** The requirement checkbox, distinguished from the export toggle. */
function requirementCheckbox(name = 'Login') {
  return screen.getByRole('checkbox', { name })
}

function exportCheckbox() {
  return screen.getByRole('checkbox', { name: /File bug findings/ })
}

function renderModal(
  onClose = vi.fn(),
  tracker: IssueTrackerConfig | null = null,
  plans: TestPlanResponse[] = [makePlan()],
) {
  const router = createMemoryRouter(
    [
      {
        path: '/sprints/:id/test-runs',
        element: <RunTestModal sprintId={1} plans={plans} tracker={tracker} onClose={onClose} />,
      },
      { path: '/sprints/:id/test-runs/:runId', element: <div>Run detail</div> },
    ],
    { initialEntries: ['/sprints/1/test-runs'] },
  )
  return render(<RouterProvider router={router} />)
}

describe('RunTestModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('lists the requirements it is given', async () => {
    // The approved-plan filter now lives in `TestRunsPage`, which owns the
    // fetch — see its own test. This asserts only that the modal renders
    // exactly the plans handed to it.
    renderModal(vi.fn(), null, [
      makePlan({ requirement_name: 'Login', status: 'approved' }),
      makePlan({ id: 11, requirement_id: 101, requirement_name: 'Search', status: 'approved' }),
    ])

    await screen.findByText('Login')
    expect(screen.getByText('Search')).toBeInTheDocument()
  })

  it('disables Start run with none checked', async () => {
    renderModal()

    await screen.findByText('Login')
    expect(screen.getByRole('button', { name: 'Start run' })).toBeDisabled()

    fireEvent.click(requirementCheckbox())
    expect(screen.getByRole('button', { name: 'Start run' })).toBeEnabled()
  })

  it('creates the run and navigates to the detail route', async () => {
    mockCreateTestRun.mockResolvedValue({
      id: 42,
      sprint_id: 1,
      created_at: '2026-01-01T00:00:00Z',
      status: 'running',
      executions: [],
    })
    renderModal()

    await screen.findByText('Login')
    fireEvent.click(requirementCheckbox())
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await waitFor(() => {
      expect(mockCreateTestRun).toHaveBeenCalledWith(1, [100], false)
    })
    expect(await screen.findByText('Run detail')).toBeInTheDocument()
  })

  it('surfaces create errors inline and keeps the modal open', async () => {
    mockCreateTestRun.mockRejectedValue(
      new Error('These requirements already have a run in progress: Login.'),
    )
    renderModal()

    await screen.findByText('Login')
    fireEvent.click(requirementCheckbox())
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    expect(
      await screen.findByText('These requirements already have a run in progress: Login.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('offers the export toggle unchecked and disabled with no tracker', async () => {
    renderModal()

    await screen.findByText('Login')

    expect(exportCheckbox()).not.toBeChecked()
    expect(exportCheckbox()).toBeDisabled()
  })

  it('checks the export toggle by default when a tracker is connected', async () => {
    // Connecting a tracker is itself the statement that findings should
    // go there — asking again per run would be asking twice.
    renderModal(vi.fn(), makeTracker())

    await screen.findByText('Login')

    expect(exportCheckbox()).toBeChecked()
    expect(exportCheckbox()).toBeEnabled()
    expect(screen.getByText(/Jira · QA/)).toBeInTheDocument()
  })

  it('sends the export flag through to the API', async () => {
    mockCreateTestRun.mockResolvedValue({ id: 42 })
    renderModal(vi.fn(), makeTracker())

    await screen.findByText('Login')
    fireEvent.click(requirementCheckbox())
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await waitFor(() => {
      expect(mockCreateTestRun).toHaveBeenCalledWith(1, [100], true)
    })
  })

  it('an unchecked toggle sends false even with a tracker connected', async () => {
    mockCreateTestRun.mockResolvedValue({ id: 42 })
    renderModal(vi.fn(), makeTracker())

    await screen.findByText('Login')
    fireEvent.click(requirementCheckbox())
    fireEvent.click(exportCheckbox())
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await waitFor(() => {
      expect(mockCreateTestRun).toHaveBeenCalledWith(1, [100], false)
    })
  })

  it('calls onClose from Cancel', async () => {
    const onClose = vi.fn()
    renderModal(onClose)

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalled()
  })

  it('is a labelled dialog that Escape closes', () => {
    const onClose = vi.fn()
    renderModal(onClose)

    // The shared shell supplies this; its full keyboard contract is covered
    // once in ConfirmModal.test.tsx rather than repeated for each modal.
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Run new test')

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
