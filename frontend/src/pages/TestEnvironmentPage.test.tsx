import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import TestEnvironmentPage from './TestEnvironmentPage'
import type { SprintResponse, TestEnvironmentResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchSprint: vi.fn(),
    fetchTestEnvironment: vi.fn(),
    submitTestEnvironment: vi.fn(),
    answerTestEnvironment: vi.fn(),
    confirmTestEnvironment: vi.fn(),
    updateTestEnvironmentVars: vi.fn(),
    finishSprint: vi.fn(),
  }
})

import {
  answerTestEnvironment,
  confirmTestEnvironment,
  fetchSprint,
  fetchTestEnvironment,
  finishSprint,
  submitTestEnvironment,
  updateTestEnvironmentVars,
} from '../services/api'

const mockFetchSprint = fetchSprint as ReturnType<typeof vi.fn>
const mockFetchTestEnvironment = fetchTestEnvironment as ReturnType<typeof vi.fn>
const mockSubmitTestEnvironment = submitTestEnvironment as ReturnType<typeof vi.fn>
const mockAnswerTestEnvironment = answerTestEnvironment as ReturnType<typeof vi.fn>
const mockConfirmTestEnvironment = confirmTestEnvironment as ReturnType<typeof vi.fn>
const mockUpdateTestEnvironmentVars = updateTestEnvironmentVars as ReturnType<typeof vi.fn>
const mockFinishSprint = finishSprint as ReturnType<typeof vi.fn>

function makeSprint(overrides: Partial<SprintResponse> = {}): SprintResponse {
  return {
    id: 1,
    name: 'Sprint 1',
    repo_id: 1,
    active: true,
    directory: 'abc123',
    created_at: '2026-01-01T00:00:00Z',
    repo: null,
    requirements_complete: true,
    has_test_environment_submission: false,
    requirements_locked: false,
    has_test_plans: false,
    test_plans_complete: false,
    has_test_runs: false,
    has_exploratory_runs: false,
    ...overrides,
  }
}

function makeTestEnv(overrides: Partial<TestEnvironmentResponse> = {}): TestEnvironmentResponse {
  return {
    id: 5,
    sprint_id: 1,
    content: 'SSH to staging as qa.',
    original_content: 'SSH to staging as qa.',
    status: 'needs_info',
    clarifying_question: 'Which host?',
    revision_count: 0,
    clarification_cap_reached: false,
    requirements_stale: false,
    env_vars: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPage(sprintId = '1') {
  const router = createMemoryRouter(
    [{ path: '/sprints/:id/test-environment', element: <TestEnvironmentPage /> }],
    { initialEntries: [`/sprints/${sprintId}/test-environment`] },
  )
  return render(<RouterProvider router={router} />)
}

describe('TestEnvironmentPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchTestEnvironment.mockResolvedValue(null)
  })

  it('shows guard notice when requirements are incomplete', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ requirements_complete: false }))
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Confirm all requirements first.')).toBeInTheDocument()
    })
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('shows finished notice on inactive sprint with no submission', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ active: false }))
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('This sprint is finished.')).toBeInTheDocument()
    })
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('submits first description and renders ready state with Confirm', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockSubmitTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'ready', clarifying_question: null }),
    )
    renderPage()

    const textarea = await screen.findByLabelText('Test environment access description')
    fireEvent.change(textarea, { target: { value: 'SSH to staging as qa.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))

    await waitFor(() => {
      expect(mockSubmitTestEnvironment).toHaveBeenCalledWith(1, 'SSH to staging as qa.')
    })
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeInTheDocument()
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })

  it('renders question and answer box on needs_info result', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockSubmitTestEnvironment.mockResolvedValue(makeTestEnv())
    renderPage()

    const textarea = await screen.findByLabelText('Test environment access description')
    fireEvent.change(textarea, { target: { value: 'SSH to staging.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))

    expect(await screen.findByText('Which host?')).toBeInTheDocument()
    expect(screen.getByLabelText('Clarification answer')).toBeInTheDocument()
  })

  it('submits an answer', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
    mockFetchTestEnvironment.mockResolvedValue(makeTestEnv())
    mockAnswerTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'ready', clarifying_question: null, revision_count: 1 }),
    )
    renderPage()

    const answerBox = await screen.findByLabelText('Clarification answer')
    fireEvent.change(answerBox, { target: { value: 'staging.example.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit answer' }))

    await waitFor(() => {
      expect(mockAnswerTestEnvironment).toHaveBeenCalledWith(5, 'staging.example.com')
    })
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeInTheDocument()
  })

  it('hides answer box and shows cap notice when cap reached', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
    mockFetchTestEnvironment.mockResolvedValue(
      makeTestEnv({ revision_count: 3, clarification_cap_reached: true }),
    )
    renderPage()

    expect(
      await screen.findByText('Clarification limit reached — edit the text directly to continue.'),
    ).toBeInTheDocument()
    expect(screen.queryByLabelText('Clarification answer')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument()
  })

  it('edit prefills the textarea and resubmits through submitTestEnvironment', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
    mockFetchTestEnvironment.mockResolvedValue(makeTestEnv())
    mockSubmitTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'ready', clarifying_question: null }),
    )
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    const textarea = screen.getByLabelText('Test environment access description')
    expect(textarea).toHaveValue('SSH to staging as qa.')
    fireEvent.change(textarea, { target: { value: 'SSH to staging as qa with key.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Resubmit' }))

    await waitFor(() => {
      expect(mockSubmitTestEnvironment).toHaveBeenCalledWith(1, 'SSH to staging as qa with key.')
    })
  })

  it('confirms a ready submission and goes read-only', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
    mockFetchTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'ready', clarifying_question: null }),
    )
    mockConfirmTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'confirmed', clarifying_question: null }),
    )
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Confirm' }))

    await waitFor(() => {
      expect(mockConfirmTestEnvironment).toHaveBeenCalledWith(5)
    })
    expect(await screen.findByText('Confirmed')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
  })

  it('stale requirements disable Confirm and Re-check re-submits current content', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
    mockFetchTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'ready', clarifying_question: null, requirements_stale: true }),
    )
    mockSubmitTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'ready', clarifying_question: null }),
    )
    renderPage()

    expect(
      await screen.findByText('Requirements changed since the last check.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Re-check' }))

    await waitFor(() => {
      expect(mockSubmitTestEnvironment).toHaveBeenCalledWith(1, 'SSH to staging as qa.')
    })
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeEnabled()
  })

  it('shows error and preserves typed text when submit fails', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint())
    mockSubmitTestEnvironment.mockRejectedValue(new Error('LLM request failed'))
    renderPage()

    const textarea = await screen.findByLabelText('Test environment access description')
    fireEvent.change(textarea, { target: { value: 'SSH to staging.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))

    expect(await screen.findByText('LLM request failed')).toBeInTheDocument()
    expect(screen.getByLabelText('Test environment access description')).toHaveValue(
      'SSH to staging.',
    )
  })

  it('shows the original toggle only when content was rewritten', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
    mockFetchTestEnvironment.mockResolvedValue(
      makeTestEnv({
        status: 'ready',
        clarifying_question: null,
        content: 'Rewritten content.',
        original_content: 'Original content.',
      }),
    )
    renderPage()

    const toggle = await screen.findByRole('button', { name: 'Show original' })
    fireEvent.click(toggle)
    expect(screen.getByText('Original content.')).toBeInTheDocument()
  })

  it('hides the original toggle when content matches original', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
    mockFetchTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'ready', clarifying_question: null }),
    )
    renderPage()

    await screen.findByRole('button', { name: 'Confirm' })
    expect(screen.queryByRole('button', { name: 'Show original' })).not.toBeInTheDocument()
  })

  it('renders read-only view on finished sprint with a submission', async () => {
    mockFetchSprint.mockResolvedValue(
      makeSprint({ active: false, has_test_environment_submission: true }),
    )
    mockFetchTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'ready', clarifying_question: null }),
    )
    renderPage()

    expect(await screen.findByText('SSH to staging as qa.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Finish Sprint' })).not.toBeInTheDocument()
  })

  it('back link points to the requirements page and Finish Sprint works', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
    mockFetchTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'ready', clarifying_question: null }),
    )
    mockFinishSprint.mockResolvedValue(makeSprint({ active: false }))
    renderPage()

    const back = await screen.findByRole('link', { name: /back to requirements/i })
    expect(back).toHaveAttribute('href', '/sprints/1')
    expect(screen.getByRole('link', { name: /back to sprints/i })).toHaveAttribute('href', '/')

    fireEvent.click(screen.getByRole('button', { name: 'Finish Sprint' }))
    await waitFor(() => {
      expect(mockFinishSprint).toHaveBeenCalledWith(1)
    })
  })

  it('shows the Continue to Test Plans link when confirmed', async () => {
    mockFetchSprint.mockResolvedValue(
      makeSprint({ has_test_environment_submission: true, requirements_locked: true }),
    )
    mockFetchTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'confirmed', clarifying_question: null }),
    )
    renderPage()

    const link = await screen.findByRole('link', { name: 'Continue to Test Plans' })
    expect(link).toHaveAttribute('href', '/sprints/1/test-plans')
  })

  it('shows the Continue link on a finished sprint with a confirmed env', async () => {
    mockFetchSprint.mockResolvedValue(
      makeSprint({ active: false, has_test_environment_submission: true }),
    )
    mockFetchTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'confirmed', clarifying_question: null }),
    )
    renderPage()

    expect(await screen.findByRole('link', { name: 'Continue to Test Plans' })).toBeInTheDocument()
  })

  it('hides the Continue link while not confirmed', async () => {
    mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
    mockFetchTestEnvironment.mockResolvedValue(
      makeTestEnv({ status: 'ready', clarifying_question: null }),
    )
    renderPage()

    await screen.findByText('SSH to staging as qa.')
    expect(screen.queryByRole('link', { name: 'Continue to Test Plans' })).not.toBeInTheDocument()
  })

  describe('environment variables', () => {
    it('renders nothing when env_vars is null', async () => {
      mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
      mockFetchTestEnvironment.mockResolvedValue(
        makeTestEnv({ status: 'ready', clarifying_question: null, env_vars: null }),
      )
      renderPage()

      await screen.findByText('SSH to staging as qa.')
      expect(screen.queryByText('Detected environment variables')).not.toBeInTheDocument()
    })

    it('renders rows when env_vars is present', async () => {
      mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
      mockFetchTestEnvironment.mockResolvedValue(
        makeTestEnv({
          status: 'ready',
          clarifying_question: null,
          env_vars: { BASE_URL: 'https://staging.example.com', PASSWORD: 'hunter2' },
        }),
      )
      renderPage()

      await screen.findByText('Detected environment variables')
      expect(screen.getByText('BASE_URL')).toBeInTheDocument()
      expect(screen.getByText(/https:\/\/staging\.example\.com/)).toBeInTheDocument()
      expect(screen.getByText('PASSWORD')).toBeInTheDocument()
    })

    it('edits, saves, and merges the response pre-confirm', async () => {
      mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
      mockFetchTestEnvironment.mockResolvedValue(
        makeTestEnv({
          status: 'ready',
          clarifying_question: null,
          env_vars: { BASE_URL: 'https://old.example.com' },
        }),
      )
      mockUpdateTestEnvironmentVars.mockResolvedValue(
        makeTestEnv({
          status: 'ready',
          clarifying_question: null,
          env_vars: { BASE_URL: 'https://correct.example.com' },
        }),
      )
      renderPage()

      fireEvent.click(await screen.findByRole('button', { name: 'Edit variables' }))
      const valueInput = screen.getByLabelText('Variable 1 value')
      fireEvent.change(valueInput, { target: { value: 'https://correct.example.com' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))

      await waitFor(() => {
        expect(mockUpdateTestEnvironmentVars).toHaveBeenCalledWith(5, {
          BASE_URL: 'https://correct.example.com',
        })
      })
      expect(await screen.findByText(/https:\/\/correct\.example\.com/)).toBeInTheDocument()
    })

    it('adds and removes rows while editing', async () => {
      mockFetchSprint.mockResolvedValue(makeSprint({ has_test_environment_submission: true }))
      mockFetchTestEnvironment.mockResolvedValue(
        makeTestEnv({
          status: 'ready',
          clarifying_question: null,
          env_vars: { BASE_URL: 'https://staging.example.com' },
        }),
      )
      mockUpdateTestEnvironmentVars.mockResolvedValue(
        makeTestEnv({
          status: 'ready',
          clarifying_question: null,
          env_vars: { BASE_URL: 'https://staging.example.com', TOKEN: 'abc123' },
        }),
      )
      renderPage()

      fireEvent.click(await screen.findByRole('button', { name: 'Edit variables' }))
      fireEvent.click(screen.getByRole('button', { name: 'Add variable' }))
      fireEvent.change(screen.getByLabelText('Variable 2 name'), { target: { value: 'TOKEN' } })
      fireEvent.change(screen.getByLabelText('Variable 2 value'), {
        target: { value: 'abc123' },
      })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))

      await waitFor(() => {
        expect(mockUpdateTestEnvironmentVars).toHaveBeenCalledWith(5, {
          BASE_URL: 'https://staging.example.com',
          TOKEN: 'abc123',
        })
      })
    })

    it('is read-only once confirmed', async () => {
      mockFetchSprint.mockResolvedValue(
        makeSprint({ has_test_environment_submission: true, requirements_locked: true }),
      )
      mockFetchTestEnvironment.mockResolvedValue(
        makeTestEnv({
          status: 'confirmed',
          clarifying_question: null,
          env_vars: { BASE_URL: 'https://staging.example.com' },
        }),
      )
      renderPage()

      await screen.findByText('Detected environment variables')
      expect(screen.getByText('BASE_URL')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Edit variables' })).not.toBeInTheDocument()
    })
  })
})
