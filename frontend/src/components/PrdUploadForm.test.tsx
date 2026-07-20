import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import PrdUploadForm from './PrdUploadForm'
import type { RequirementResponse } from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    uploadPrd: vi.fn(),
  }
})

import { uploadPrd } from '../services/api'

const mockUploadPrd = uploadPrd as ReturnType<typeof vi.fn>

const createdRow: RequirementResponse = {
  id: 1,
  sprint_id: 1,
  name: 'Login',
  description: 'Users can log in.',
  original_description: 'Users can log in.',
  from_prd: true,
  status: 'pending',
  clarifying_question: null,
  revision_count: 0,
  clarification_cap_reached: false,
  error: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function selectFile(name = 'prd.md') {
  const file = new File(['# PRD'], name, { type: 'text/markdown' })
  fireEvent.change(screen.getByLabelText(/prd file/i), { target: { files: [file] } })
  return file
}

describe('PrdUploadForm', () => {
  const onUploaded = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  function renderForm(hasPrdRequirements = false) {
    return render(
      <PrdUploadForm
        sprintId={1}
        hasPrdRequirements={hasPrdRequirements}
        onUploaded={onUploaded}
      />,
    )
  }

  it('disables the button until a file is selected', () => {
    renderForm()
    const button = screen.getByRole('button', { name: /upload prd/i })
    expect(button).toBeDisabled()
    selectFile()
    expect(button).toBeEnabled()
  })

  it('uploads the selected file and reports created rows', async () => {
    mockUploadPrd.mockResolvedValue([createdRow])
    renderForm()
    const file = selectFile()

    fireEvent.click(screen.getByRole('button', { name: /upload prd/i }))

    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith([createdRow]))
    expect(mockUploadPrd).toHaveBeenCalledWith(1, file)
  })

  it('shows busy copy while the split runs', async () => {
    let resolve!: (rows: RequirementResponse[]) => void
    mockUploadPrd.mockReturnValue(new Promise((res) => (resolve = res)))
    renderForm()
    selectFile()

    fireEvent.click(screen.getByRole('button', { name: /upload prd/i }))

    expect(screen.getByText(/splitting prd into requirements/i)).toBeDisabled()
    resolve([createdRow])
    await waitFor(() => expect(onUploaded).toHaveBeenCalled())
  })

  it('renders API errors', async () => {
    mockUploadPrd.mockRejectedValue(new Error('No requirements could be found'))
    renderForm()
    selectFile()

    fireEvent.click(screen.getByRole('button', { name: /upload prd/i }))

    expect(await screen.findByText(/no requirements could be found/i)).toBeInTheDocument()
    expect(onUploaded).not.toHaveBeenCalled()
  })

  it('asks for confirmation before replacing an earlier upload', async () => {
    mockUploadPrd.mockResolvedValue([createdRow])
    renderForm(true)
    selectFile()

    fireEvent.click(screen.getByRole('button', { name: /upload prd/i }))

    expect(mockUploadPrd).not.toHaveBeenCalled()
    expect(screen.getByText(/replaces them/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /replace requirements/i }))
    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith([createdRow]))
  })

  it('cancelling the confirmation aborts the upload', () => {
    renderForm(true)
    selectFile()

    fireEvent.click(screen.getByRole('button', { name: /upload prd/i }))
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))

    expect(mockUploadPrd).not.toHaveBeenCalled()
    expect(screen.queryByText(/replaces them/i)).not.toBeInTheDocument()
  })

  it('skips confirmation when no PRD requirements exist', async () => {
    mockUploadPrd.mockResolvedValue([createdRow])
    renderForm(false)
    selectFile()

    fireEvent.click(screen.getByRole('button', { name: /upload prd/i }))

    expect(screen.queryByText(/replaces them/i)).not.toBeInTheDocument()
    await waitFor(() => expect(onUploaded).toHaveBeenCalled())
  })
})
