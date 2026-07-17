import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import TestPlanEditForm from './TestPlanEditForm'
import type { TestPlanResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    updateTestPlan: vi.fn(),
  }
})

import { updateTestPlan } from '../services/api'

const mockUpdateTestPlan = updateTestPlan as ReturnType<typeof vi.fn>

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
    ],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderForm(plan = makePlan(), onSaved = vi.fn(), onCancel = vi.fn()) {
  render(<TestPlanEditForm plan={plan} onSaved={onSaved} onCancel={onCancel} />)
  return { onSaved, onCancel }
}

describe('TestPlanEditForm', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('prefills all fields from the plan', () => {
    renderForm()

    expect(screen.getByLabelText('Complexity')).toHaveValue('medium')
    expect(screen.getByLabelText('Summary')).toHaveValue('Covers the login flows.')
    expect(screen.getByLabelText('Title')).toHaveValue('Valid login')
    expect(screen.getByLabelText('Preconditions (optional)')).toHaveValue(
      'A registered user exists.',
    )
    expect(screen.getByLabelText('Steps (one per line)')).toHaveValue(
      'Open the login page\nSubmit valid credentials',
    )
    expect(screen.getByLabelText('Expected result')).toHaveValue('User lands on the dashboard.')
    expect(screen.getByLabelText('Type')).toHaveValue('functional')
    expect(screen.getByLabelText('Priority')).toHaveValue('high')
  })

  it('saves the edited plan with steps kept one-per-line', async () => {
    const saved = makePlan({ summary: 'New summary' })
    mockUpdateTestPlan.mockResolvedValue(saved)
    const { onSaved } = renderForm()

    fireEvent.change(screen.getByLabelText('Summary'), { target: { value: 'New summary' } })
    fireEvent.change(screen.getByLabelText('Steps (one per line)'), {
      target: { value: 'Step A\nStep B\nStep C' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(mockUpdateTestPlan).toHaveBeenCalledWith(10, {
        complexity: 'medium',
        summary: 'New summary',
        cases: [
          {
            title: 'Valid login',
            preconditions: 'A registered user exists.',
            steps: 'Step A\nStep B\nStep C',
            expected_result: 'User lands on the dashboard.',
            case_type: 'functional',
            priority: 'high',
          },
        ],
      })
    })
    expect(onSaved).toHaveBeenCalledWith(saved)
  })

  it('adds and removes cases', () => {
    renderForm()

    fireEvent.click(screen.getByRole('button', { name: 'Add case' }))
    expect(screen.getAllByLabelText('Title')).toHaveLength(2)

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove case' })[1])
    expect(screen.getAllByLabelText('Title')).toHaveLength(1)
  })

  it('cannot remove the last case', () => {
    renderForm()

    expect(screen.getByRole('button', { name: 'Remove case' })).toBeDisabled()
  })

  it('disables Save while a case is invalid', () => {
    renderForm()

    fireEvent.change(screen.getByLabelText('Title'), { target: { value: '   ' } })

    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(mockUpdateTestPlan).not.toHaveBeenCalled()
  })

  it('sends null for blank preconditions', async () => {
    mockUpdateTestPlan.mockResolvedValue(makePlan())
    renderForm()

    fireEvent.change(screen.getByLabelText('Preconditions (optional)'), {
      target: { value: '   ' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(mockUpdateTestPlan).toHaveBeenCalled()
    })
    expect(mockUpdateTestPlan.mock.calls[0][1].cases[0].preconditions).toBeNull()
  })

  it('calls onCancel without saving', () => {
    const { onCancel } = renderForm()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(onCancel).toHaveBeenCalled()
    expect(mockUpdateTestPlan).not.toHaveBeenCalled()
  })

  it('surfaces save errors and stays editable', async () => {
    mockUpdateTestPlan.mockRejectedValue(new Error('Only draft plans can be edited.'))
    const { onSaved } = renderForm()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('Only draft plans can be edited.')).toBeInTheDocument()
    expect(onSaved).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })
})
