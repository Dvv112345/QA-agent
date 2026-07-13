import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import RequirementCard from './RequirementCard'
import type { RequirementResponse, RequirementStatus } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    answerRequirement: vi.fn(),
    confirmRequirement: vi.fn(),
    updateRequirement: vi.fn(),
    restartRequirement: vi.fn(),
    deleteRequirement: vi.fn(),
  }
})

import {
  answerRequirement,
  confirmRequirement,
  deleteRequirement,
  restartRequirement,
  updateRequirement,
} from '../services/api'

const mockAnswer = answerRequirement as ReturnType<typeof vi.fn>
const mockConfirm = confirmRequirement as ReturnType<typeof vi.fn>
const mockUpdate = updateRequirement as ReturnType<typeof vi.fn>
const mockRestart = restartRequirement as ReturnType<typeof vi.fn>
const mockDelete = deleteRequirement as ReturnType<typeof vi.fn>

function makeRequirement(overrides: Partial<RequirementResponse> = {}): RequirementResponse {
  return {
    id: 7,
    sprint_id: 1,
    name: 'Login',
    description: 'Users can log in.',
    original_description: 'Users can log in.',
    status: 'ready',
    clarifying_question: null,
    revision_count: 0,
    error: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderCard(requirement: RequirementResponse, props: Record<string, unknown> = {}) {
  return render(
    <RequirementCard
      requirement={requirement}
      sprintActive={true}
      onUpdated={vi.fn()}
      onRemoved={vi.fn()}
      {...props}
    />,
  )
}

describe('RequirementCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  describe('status rendering', () => {
    it.each<[RequirementStatus, string]>([
      ['pending', 'Queued'],
      ['analyzing', 'Analyzing'],
      ['needs_clarification', 'Needs clarification'],
      ['ready', 'Ready'],
      ['confirmed', 'Confirmed'],
      ['failed', 'Failed'],
    ])('shows the %s badge', (status, label) => {
      renderCard(makeRequirement({ status }))
      expect(screen.getByText(label)).toBeInTheDocument()
    })

    it('shows the clarifying question with an answer box', () => {
      renderCard(
        makeRequirement({
          status: 'needs_clarification',
          clarifying_question: 'Which users?',
        }),
      )
      expect(screen.getByText('Which users?')).toBeInTheDocument()
      expect(screen.getByLabelText('Clarification answer')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Confirm as-is' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument()
    })

    it('replaces the answer box with a notice at the clarification cap', () => {
      renderCard(
        makeRequirement({
          status: 'needs_clarification',
          clarifying_question: 'Which users?',
          revision_count: 3,
        }),
      )
      expect(screen.getByText(/clarification limit reached/i)).toBeInTheDocument()
      expect(screen.queryByLabelText('Clarification answer')).not.toBeInTheDocument()
      // Confirm and Edit remain available
      expect(screen.getByRole('button', { name: 'Confirm as-is' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument()
    })

    it('shows a toggle for the original description when rewritten', () => {
      renderCard(
        makeRequirement({
          description: 'Rewritten text.',
          original_description: 'Original text.',
        }),
      )
      expect(screen.queryByText('Original text.')).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Show original' }))
      expect(screen.getByText('Original text.')).toBeInTheDocument()
    })

    it('shows the stored error and a Restart button when failed', () => {
      renderCard(makeRequirement({ status: 'failed', error: 'LLM exploded' }))
      expect(screen.getByText('LLM exploded')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Restart' })).toBeInTheDocument()
    })

    it('renders confirmed cards with Remove as the only action', () => {
      renderCard(makeRequirement({ status: 'confirmed' }))
      expect(screen.getByRole('button', { name: 'Remove' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    })

    it('shows Remove even while analyzing', () => {
      renderCard(makeRequirement({ status: 'analyzing' }))
      expect(screen.getByRole('button', { name: 'Remove' })).toBeInTheDocument()
    })

    it('hides all actions on finished sprints', () => {
      renderCard(makeRequirement({ status: 'needs_clarification', clarifying_question: 'Q?' }), {
        sprintActive: false,
      })
      expect(screen.queryByRole('button')).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Clarification answer')).not.toBeInTheDocument()
    })
  })

  describe('actions', () => {
    it('submits an answer', async () => {
      const onUpdated = vi.fn()
      const updated = makeRequirement({ status: 'pending' })
      mockAnswer.mockResolvedValue(updated)
      renderCard(makeRequirement({ status: 'needs_clarification', clarifying_question: 'Q?' }), {
        onUpdated,
      })

      fireEvent.change(screen.getByLabelText('Clarification answer'), {
        target: { value: 'All users.' },
      })
      fireEvent.click(screen.getByRole('button', { name: 'Submit answer' }))

      await waitFor(() => {
        expect(mockAnswer).toHaveBeenCalledWith(7, 'All users.')
      })
      expect(onUpdated).toHaveBeenCalledWith(updated)
    })

    it('confirms a ready requirement', async () => {
      const onUpdated = vi.fn()
      const updated = makeRequirement({ status: 'confirmed' })
      mockConfirm.mockResolvedValue(updated)
      renderCard(makeRequirement({ status: 'ready' }), { onUpdated })

      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

      await waitFor(() => {
        expect(mockConfirm).toHaveBeenCalledWith(7)
      })
      expect(onUpdated).toHaveBeenCalledWith(updated)
    })

    it('saves an edited description', async () => {
      const onUpdated = vi.fn()
      const updated = makeRequirement({ status: 'pending', description: 'New text.' })
      mockUpdate.mockResolvedValue(updated)
      renderCard(makeRequirement({ status: 'ready' }), { onUpdated })

      fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
      fireEvent.change(screen.getByLabelText('Edit description'), {
        target: { value: 'New text.' },
      })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))

      await waitFor(() => {
        expect(mockUpdate).toHaveBeenCalledWith(7, 'New text.')
      })
      expect(onUpdated).toHaveBeenCalledWith(updated)
    })

    it('cancels editing without an API call', () => {
      renderCard(makeRequirement({ status: 'ready' }))

      fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

      expect(mockUpdate).not.toHaveBeenCalled()
      expect(screen.queryByLabelText('Edit description')).not.toBeInTheDocument()
    })

    it('restarts a failed requirement', async () => {
      const onUpdated = vi.fn()
      const updated = makeRequirement({ status: 'pending' })
      mockRestart.mockResolvedValue(updated)
      renderCard(makeRequirement({ status: 'failed', error: 'boom' }), { onUpdated })

      fireEvent.click(screen.getByRole('button', { name: 'Restart' }))

      await waitFor(() => {
        expect(mockRestart).toHaveBeenCalledWith(7)
      })
      expect(onUpdated).toHaveBeenCalledWith(updated)
    })

    it.each<RequirementStatus>([
      'pending',
      'analyzing',
      'needs_clarification',
      'ready',
      'confirmed',
      'failed',
    ])('removes a %s requirement after confirmation', async (status) => {
      const onRemoved = vi.fn()
      mockDelete.mockResolvedValue(undefined)
      renderCard(makeRequirement({ status, clarifying_question: 'Q?', error: 'e' }), { onRemoved })

      fireEvent.click(screen.getByRole('button', { name: 'Remove' }))

      await waitFor(() => {
        expect(mockDelete).toHaveBeenCalledWith(7)
      })
      expect(onRemoved).toHaveBeenCalledWith(7)
    })

    it('does not remove when the confirmation is dismissed', () => {
      vi.spyOn(window, 'confirm').mockReturnValue(false)
      renderCard(makeRequirement({ status: 'ready' }))

      fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
      expect(mockDelete).not.toHaveBeenCalled()
    })

    it('shows action errors inline on the card', async () => {
      mockConfirm.mockRejectedValue(new Error('Sprint is finished'))
      renderCard(makeRequirement({ status: 'ready' }))

      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

      await waitFor(() => {
        expect(screen.getByText('Sprint is finished')).toBeInTheDocument()
      })
    })
  })
})
