import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import RequirementForm from './RequirementForm'
import type { RequirementResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    submitRequirements: vi.fn(),
  }
})

import { submitRequirements } from '../services/api'

const mockSubmitRequirements = submitRequirements as ReturnType<typeof vi.fn>

const createdRow: RequirementResponse = {
  id: 1,
  sprint_id: 1,
  name: 'Login',
  description: 'Users can log in.',
  original_description: 'Users can log in.',
  from_prd: false,
  status: 'pending',
  clarifying_question: null,
  revision_count: 0,
  clarification_cap_reached: false,
  error: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

describe('RequirementForm', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('starts with one empty row and disabled submit', () => {
    render(<RequirementForm sprintId={1} onSubmitted={vi.fn()} />)
    expect(screen.getByLabelText('Requirement 1 name')).toBeInTheDocument()
    expect(screen.queryByLabelText('Requirement 2 name')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Submit Requirements' })).toBeDisabled()
  })

  it('adds and removes rows (never below one)', () => {
    render(<RequirementForm sprintId={1} onSubmitted={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: '+ Add requirement' }))
    expect(screen.getByLabelText('Requirement 2 name')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Remove row 2' }))
    expect(screen.queryByLabelText('Requirement 2 name')).not.toBeInTheDocument()

    // Last remaining row cannot be removed
    expect(screen.getByRole('button', { name: 'Remove row 1' })).toBeDisabled()
  })

  it('submits trimmed rows and notifies the parent', async () => {
    const onSubmitted = vi.fn()
    mockSubmitRequirements.mockResolvedValue([createdRow])
    render(<RequirementForm sprintId={1} onSubmitted={onSubmitted} />)

    fireEvent.change(screen.getByLabelText('Requirement 1 name'), {
      target: { value: '  Login  ' },
    })
    fireEvent.change(screen.getByLabelText('Requirement 1 description'), {
      target: { value: '  Users can log in.  ' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Submit Requirements' }))

    await waitFor(() => {
      expect(mockSubmitRequirements).toHaveBeenCalledWith(1, [
        { name: 'Login', description: 'Users can log in.' },
      ])
    })
    expect(onSubmitted).toHaveBeenCalledWith([createdRow])
    // Form resets after successful submit
    expect(screen.getByLabelText('Requirement 1 name')).toHaveValue('')
  })

  it('skips fully blank rows on submit', async () => {
    mockSubmitRequirements.mockResolvedValue([createdRow])
    render(<RequirementForm sprintId={1} onSubmitted={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: '+ Add requirement' }))
    fireEvent.change(screen.getByLabelText('Requirement 1 name'), {
      target: { value: 'Login' },
    })
    fireEvent.change(screen.getByLabelText('Requirement 1 description'), {
      target: { value: 'Users can log in.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Submit Requirements' }))

    await waitFor(() => {
      expect(mockSubmitRequirements).toHaveBeenCalledWith(1, [
        { name: 'Login', description: 'Users can log in.' },
      ])
    })
  })

  it('shows the API error inline', async () => {
    mockSubmitRequirements.mockRejectedValue(new Error('Sprint is finished'))
    render(<RequirementForm sprintId={1} onSubmitted={vi.fn()} />)

    fireEvent.change(screen.getByLabelText('Requirement 1 name'), {
      target: { value: 'Login' },
    })
    fireEvent.change(screen.getByLabelText('Requirement 1 description'), {
      target: { value: 'desc' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Submit Requirements' }))

    await waitFor(() => {
      expect(screen.getByText('Sprint is finished')).toBeInTheDocument()
    })
  })
})
