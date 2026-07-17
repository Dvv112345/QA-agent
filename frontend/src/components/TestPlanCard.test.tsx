import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import TestPlanCard from './TestPlanCard'
import type { TestPlanResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    submitTestPlanFeedback: vi.fn(),
    updateTestPlan: vi.fn(),
    approveTestPlan: vi.fn(),
    restartTestPlan: vi.fn(),
  }
})

import {
  approveTestPlan,
  restartTestPlan,
  submitTestPlanFeedback,
  updateTestPlan,
} from '../services/api'

const mockSubmitFeedback = submitTestPlanFeedback as ReturnType<typeof vi.fn>
const mockUpdateTestPlan = updateTestPlan as ReturnType<typeof vi.fn>
const mockApprove = approveTestPlan as ReturnType<typeof vi.fn>
const mockRestart = restartTestPlan as ReturnType<typeof vi.fn>

function makePlan(overrides: Partial<TestPlanResponse> = {}): TestPlanResponse {
  return {
    id: 10,
    requirement_id: 100,
    requirement_name: 'Login',
    requirement_description: 'Users can log in.',
    status: 'draft',
    complexity: 'medium',
    summary: 'Covers the login flows.',
    revision_count: 0,
    feedback_cap_reached: false,
    error: null,
    cases: [
      {
        id: 1,
        position: 0,
        title: 'Valid login',
        preconditions: 'A registered user exists.',
        steps: 'Open the login page\nSubmit valid credentials',
        expected_result: 'User lands on the dashboard.',
        case_type: 'functional',
        priority: 'high',
      },
      {
        id: 2,
        position: 1,
        title: 'Invalid login',
        preconditions: null,
        steps: 'Open the login page\nEnter a wrong password',
        expected_result: 'An error message is shown.',
        case_type: 'negative',
        priority: 'medium',
      },
    ],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderCard(plan: TestPlanResponse, sprintActive = true, onUpdated = vi.fn()) {
  render(<TestPlanCard plan={plan} sprintActive={sprintActive} onUpdated={onUpdated} />)
  return onUpdated
}

describe('TestPlanCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  it('renders summary, badges, and cases with numbered steps', () => {
    renderCard(makePlan())

    expect(screen.getByText('Login')).toBeInTheDocument()
    expect(screen.getByText('medium complexity')).toBeInTheDocument()
    expect(screen.getByText('Draft')).toBeInTheDocument()
    expect(screen.getByText('Covers the login flows.')).toBeInTheDocument()
    expect(screen.getByText('Valid login')).toBeInTheDocument()
    // "Open the login page" is the first step of both cases
    expect(screen.getAllByText('Open the login page', { selector: 'li' })).toHaveLength(2)
    expect(screen.getByText('Submit valid credentials')).toBeInTheDocument()
    expect(screen.getByText(/A registered user exists./)).toBeInTheDocument()
    expect(screen.getByText(/User lands on the dashboard./)).toBeInTheDocument()
    expect(screen.getByText('functional test')).toBeInTheDocument()
    expect(screen.getByText('high priority')).toBeInTheDocument()
  })

  it('toggles the requirement description', () => {
    renderCard(makePlan())

    expect(screen.queryByText('Users can log in.')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show requirement' }))
    expect(screen.getByText('Users can log in.')).toBeInTheDocument()
  })

  it('shows a spinner for in-progress plans', () => {
    renderCard(makePlan({ status: 'generating', cases: [] }))

    expect(screen.getByText('Generating test plan…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
  })

  it('submits feedback and clears the textarea', async () => {
    const updated = makePlan({ status: 'pending' })
    mockSubmitFeedback.mockResolvedValue(updated)
    const onUpdated = renderCard(makePlan())

    fireEvent.change(screen.getByLabelText('Test plan feedback'), {
      target: { value: 'Add lockout cases.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send feedback' }))

    await waitFor(() => {
      expect(mockSubmitFeedback).toHaveBeenCalledWith(10, 'Add lockout cases.')
    })
    expect(onUpdated).toHaveBeenCalledWith(updated)
    expect(screen.getByLabelText('Test plan feedback')).toHaveValue('')
  })

  it('hides feedback and shows the cap notice past the cap', () => {
    renderCard(makePlan({ feedback_cap_reached: true }))

    expect(screen.queryByLabelText('Test plan feedback')).not.toBeInTheDocument()
    expect(screen.getByText('Feedback limit reached — edit the plan directly.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument()
  })

  it('approves after confirmation', async () => {
    const approved = makePlan({ status: 'approved' })
    mockApprove.mockResolvedValue(approved)
    const onUpdated = renderCard(makePlan())

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    await waitFor(() => {
      expect(mockApprove).toHaveBeenCalledWith(10)
    })
    expect(onUpdated).toHaveBeenCalledWith(approved)
  })

  it('does not approve when confirmation is cancelled', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderCard(makePlan())

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    expect(mockApprove).not.toHaveBeenCalled()
  })

  it('shows error and Restart for failed plans', async () => {
    const restarted = makePlan({ status: 'pending', error: null, cases: [] })
    mockRestart.mockResolvedValue(restarted)
    const onUpdated = renderCard(makePlan({ status: 'failed', error: 'LLM exploded', cases: [] }))

    expect(screen.getByText('LLM exploded')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Restart' }))

    await waitFor(() => {
      expect(mockRestart).toHaveBeenCalledWith(10)
    })
    expect(onUpdated).toHaveBeenCalledWith(restarted)
  })

  it('renders approved plans read-only', () => {
    renderCard(makePlan({ status: 'approved' }))

    expect(screen.getByText('Approved')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Test plan feedback')).not.toBeInTheDocument()
  })

  it('renders draft plans read-only when the sprint is inactive', () => {
    renderCard(makePlan(), false)

    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Test plan feedback')).not.toBeInTheDocument()
  })

  it('hides Restart on failed plans when the sprint is inactive', () => {
    renderCard(makePlan({ status: 'failed', error: 'boom', cases: [] }), false)

    expect(screen.queryByRole('button', { name: 'Restart' })).not.toBeInTheDocument()
  })

  it('opens the edit form, saves, and returns to the read view', async () => {
    const edited = makePlan({ summary: 'Edited.' })
    mockUpdateTestPlan.mockResolvedValue(edited)
    const onUpdated = renderCard(makePlan())

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(screen.getByLabelText('Summary')).toHaveValue('Covers the login flows.')

    fireEvent.change(screen.getByLabelText('Summary'), { target: { value: 'Edited.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(mockUpdateTestPlan).toHaveBeenCalled()
    })
    expect(onUpdated).toHaveBeenCalledWith(edited)
    expect(screen.queryByLabelText('Summary')).not.toBeInTheDocument()
  })

  it('cancel closes the edit form without saving', () => {
    renderCard(makePlan())

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(mockUpdateTestPlan).not.toHaveBeenCalled()
    expect(screen.queryByLabelText('Summary')).not.toBeInTheDocument()
    expect(screen.getByText('Covers the login flows.')).toBeInTheDocument()
  })
})
