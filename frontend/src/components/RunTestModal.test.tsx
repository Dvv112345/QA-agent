import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import RunTestModal from './RunTestModal'
import type { TestPlanResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchTestPlans: vi.fn(),
    createTestRun: vi.fn(),
  }
})

import { createTestRun, fetchTestPlans } from '../services/api'

const mockFetchTestPlans = fetchTestPlans as ReturnType<typeof vi.fn>
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

function renderModal(onClose = vi.fn()) {
  const router = createMemoryRouter(
    [
      { path: '/sprints/:id/test-runs', element: <RunTestModal sprintId={1} onClose={onClose} /> },
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

  it('lists only requirements with an approved plan', async () => {
    mockFetchTestPlans.mockResolvedValue([
      makePlan({ requirement_name: 'Login', status: 'approved' }),
      makePlan({
        id: 11,
        requirement_id: 101,
        requirement_name: 'Search',
        status: 'draft',
      }),
    ])
    renderModal()

    await screen.findByText('Login')
    expect(screen.queryByText('Search')).not.toBeInTheDocument()
  })

  it('disables Start run with none checked', async () => {
    mockFetchTestPlans.mockResolvedValue([makePlan()])
    renderModal()

    await screen.findByText('Login')
    expect(screen.getByRole('button', { name: 'Start run' })).toBeDisabled()

    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: 'Start run' })).toBeEnabled()
  })

  it('creates the run and navigates to the detail route', async () => {
    mockFetchTestPlans.mockResolvedValue([makePlan()])
    mockCreateTestRun.mockResolvedValue({
      id: 42,
      sprint_id: 1,
      created_at: '2026-01-01T00:00:00Z',
      status: 'running',
      executions: [],
    })
    renderModal()

    fireEvent.click(await screen.findByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await waitFor(() => {
      expect(mockCreateTestRun).toHaveBeenCalledWith(1, [100])
    })
    expect(await screen.findByText('Run detail')).toBeInTheDocument()
  })

  it('surfaces create errors inline and keeps the modal open', async () => {
    mockFetchTestPlans.mockResolvedValue([makePlan()])
    mockCreateTestRun.mockRejectedValue(
      new Error('These requirements already have a run in progress: Login.'),
    )
    renderModal()

    fireEvent.click(await screen.findByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    expect(
      await screen.findByText('These requirements already have a run in progress: Login.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('calls onClose from Cancel', async () => {
    const onClose = vi.fn()
    mockFetchTestPlans.mockResolvedValue([makePlan()])
    renderModal(onClose)

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalled()
  })
})
