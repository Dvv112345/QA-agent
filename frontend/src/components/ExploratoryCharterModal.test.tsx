import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import ExploratoryCharterModal from './ExploratoryCharterModal'
import type {
  ExploratoryCharterDraftResponse,
  IssueTrackerConfig,
  TestPlanResponse,
} from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchTestPlans: vi.fn(),
    generateCharters: vi.fn(),
    createExploratoryRun: vi.fn(),
  }
})

import { createExploratoryRun, fetchTestPlans, generateCharters } from '../services/api'

const mockFetchTestPlans = fetchTestPlans as ReturnType<typeof vi.fn>
const mockGenerateCharters = generateCharters as ReturnType<typeof vi.fn>
const mockCreateRun = createExploratoryRun as ReturnType<typeof vi.fn>

function makePlan(overrides: Partial<TestPlanResponse> = {}): TestPlanResponse {
  return {
    id: 1,
    requirement_id: 11,
    requirement_name: 'Export reports',
    requirement_description: 'Users can export reports as CSV',
    status: 'approved',
    complexity: 'medium',
    summary: 'Plan',
    revision_count: 0,
    feedback_cap_reached: false,
    error: null,
    cases: [],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeDraft(
  overrides: Partial<ExploratoryCharterDraftResponse> = {},
): ExploratoryCharterDraftResponse {
  return {
    requirement_id: 11,
    requirement_name: 'Export reports',
    charters: [
      { charter: 'Explore export triggers', sfdipot_areas: ['Function'] },
      { charter: 'Explore export edge data', sfdipot_areas: ['Data'] },
    ],
    base_url_env_vars: ['APP_URL'],
    charter_count: 2,
    projected_minutes: 14,
    ...overrides,
  }
}

function makeTracker(overrides: Partial<IssueTrackerConfig> = {}): IssueTrackerConfig {
  return {
    id: 1,
    sprint_id: 1,
    provider: 'github',
    target: 'acme/shop',
    target_label: 'GitHub · acme/shop',
    base_url: null,
    account_email: null,
    issue_type: null,
    verified_at: '2026-01-01T00:00:00Z',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function exportCheckbox() {
  return screen.getByRole('checkbox', { name: /File bug findings/ })
}

function renderModal(onClose = vi.fn(), tracker: IssueTrackerConfig | null = null) {
  const router = createMemoryRouter(
    [
      {
        path: '/',
        element: <ExploratoryCharterModal sprintId={1} tracker={tracker} onClose={onClose} />,
      },
      { path: '*', element: <div>navigated</div> },
    ],
    { initialEntries: ['/'] },
  )
  return render(<RouterProvider router={router} />)
}

describe('ExploratoryCharterModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchTestPlans.mockResolvedValue([makePlan()])
    mockGenerateCharters.mockResolvedValue(makeDraft())
  })

  it('lists requirements with an approved plan as single-select radios', async () => {
    mockFetchTestPlans.mockResolvedValue([
      makePlan(),
      makePlan({ id: 2, requirement_id: 12, requirement_name: 'Login', status: 'draft' }),
    ])
    renderModal()

    const radios = await screen.findAllByRole('radio')
    expect(radios).toHaveLength(1)
    expect(screen.getByText('Export reports')).toBeInTheDocument()
    expect(screen.queryByText('Login')).not.toBeInTheDocument()
  })

  it('shows an empty state when no plan is approved', async () => {
    mockFetchTestPlans.mockResolvedValue([])
    renderModal()

    expect(
      await screen.findByText('No requirements have an approved test plan yet.'),
    ).toBeInTheDocument()
  })

  it('generates charters for the selected requirement', async () => {
    renderModal()

    fireEvent.click(await screen.findByRole('button', { name: 'Generate charters' }))

    await waitFor(() => expect(mockGenerateCharters).toHaveBeenCalledWith(1, 11))
    expect(await screen.findByDisplayValue('Explore export triggers')).toBeInTheDocument()
  })

  it('shows the server-supplied cost projection on the confirm button', async () => {
    renderModal()
    fireEvent.click(await screen.findByRole('button', { name: 'Generate charters' }))

    expect(await screen.findByRole('button', { name: /~14 min/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Start 2 sessions/ })).toBeInTheDocument()
  })

  it('displays the nominated base URL variables for review', async () => {
    renderModal()
    fireEvent.click(await screen.findByRole('button', { name: 'Generate charters' }))

    expect(await screen.findByText(/APP_URL/)).toBeInTheDocument()
  })

  it('names the starting URL, since each session opens on the first one', async () => {
    mockGenerateCharters.mockResolvedValue(makeDraft({ base_url_env_vars: ['APP_URL', 'API_URL'] }))
    renderModal()
    fireEvent.click(await screen.findByRole('button', { name: 'Generate charters' }))

    expect(await screen.findByText(/Exploration starts at/)).toBeInTheDocument()
    expect(screen.getByText(/also reachable: API_URL/)).toBeInTheDocument()
  })

  it('submits edited charter text', async () => {
    mockCreateRun.mockResolvedValue({ id: 99 })
    renderModal()
    fireEvent.click(await screen.findByRole('button', { name: 'Generate charters' }))

    const textarea = await screen.findByDisplayValue('Explore export triggers')
    fireEvent.change(textarea, { target: { value: 'Explore export permissions' } })
    fireEvent.click(screen.getByRole('button', { name: /Start 2 sessions/ }))

    await waitFor(() =>
      expect(mockCreateRun).toHaveBeenCalledWith(
        1,
        11,
        [
          { charter: 'Explore export permissions', sfdipot_areas: ['Function'] },
          { charter: 'Explore export edge data', sfdipot_areas: ['Data'] },
        ],
        ['APP_URL'],
        false,
      ),
    )
  })

  it('offers the export toggle only once charters exist', async () => {
    // Before generating there is no run to configure — the checkbox would
    // be a setting for something that may never be created.
    renderModal(vi.fn(), makeTracker())

    expect(screen.queryByRole('checkbox', { name: /File bug findings/ })).not.toBeInTheDocument()

    fireEvent.click(await screen.findByRole('button', { name: 'Generate charters' }))
    await screen.findByDisplayValue('Explore export triggers')

    expect(exportCheckbox()).toBeInTheDocument()
  })

  it('checks the export toggle by default when a tracker is connected', async () => {
    renderModal(vi.fn(), makeTracker())
    fireEvent.click(await screen.findByRole('button', { name: 'Generate charters' }))
    await screen.findByDisplayValue('Explore export triggers')

    expect(exportCheckbox()).toBeChecked()
    expect(screen.getByText(/GitHub · acme\/shop/)).toBeInTheDocument()
  })

  it('disables the export toggle with no tracker connected', async () => {
    renderModal()
    fireEvent.click(await screen.findByRole('button', { name: 'Generate charters' }))
    await screen.findByDisplayValue('Explore export triggers')

    expect(exportCheckbox()).toBeDisabled()
    expect(exportCheckbox()).not.toBeChecked()
  })

  it('sends the export flag through to the API', async () => {
    mockCreateRun.mockResolvedValue({ id: 99 })
    renderModal(vi.fn(), makeTracker())
    fireEvent.click(await screen.findByRole('button', { name: 'Generate charters' }))
    await screen.findByDisplayValue('Explore export triggers')

    fireEvent.click(screen.getByRole('button', { name: /Start 2 sessions/ }))

    await waitFor(() =>
      expect(mockCreateRun).toHaveBeenCalledWith(1, 11, expect.anything(), ['APP_URL'], true),
    )
  })

  it('rescales the estimate when a charter is removed', async () => {
    renderModal()
    fireEvent.click(await screen.findByRole('button', { name: 'Generate charters' }))

    await screen.findByDisplayValue('Explore export triggers')
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0])

    expect(
      await screen.findByRole('button', { name: /Start 1 session .*~7 min/ }),
    ).toBeInTheDocument()
  })

  it('disables starting when a charter is blank', async () => {
    renderModal()
    fireEvent.click(await screen.findByRole('button', { name: 'Generate charters' }))

    const textarea = await screen.findByDisplayValue('Explore export triggers')
    fireEvent.change(textarea, { target: { value: '  ' } })

    expect(screen.getByRole('button', { name: /Start 2 sessions/ })).toBeDisabled()
  })

  it('surfaces a generation error', async () => {
    mockGenerateCharters.mockRejectedValue(new Error('provider exploded'))
    renderModal()

    fireEvent.click(await screen.findByRole('button', { name: 'Generate charters' }))

    expect(await screen.findByText('provider exploded')).toBeInTheDocument()
  })
})
