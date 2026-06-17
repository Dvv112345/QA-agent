import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithRouter } from '../test/test-utils'
import HomePage from './HomePage'

// react-router-dom mock — useNavigate returns our mock so we can assert on it
const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})

function getZipInput() {
  return screen.getByLabelText(/source code/i)
}

function getMdInput() {
  return screen.getByLabelText(/requirements/i)
}

function getSubmitButton() {
  return screen.getByRole('button', { name: /upload & analyze/i })
}

function createFile(name: string, type: string) {
  return new File(['dummy'], name, { type })
}

describe('HomePage', () => {
  it('renders two file inputs and a submit button', () => {
    renderWithRouter(<HomePage />)

    expect(getZipInput()).toBeInTheDocument()
    expect(getMdInput()).toBeInTheDocument()
    expect(getSubmitButton()).toBeInTheDocument()
  })

  it('shows "Expected .zip" error when a non-zip file is selected in zip input', async () => {
    renderWithRouter(<HomePage />)

    await fireEvent.change(getZipInput(), {
      target: { files: [createFile('test.txt', 'text/plain')] },
    })

    expect(screen.getByRole('alert')).toHaveTextContent('Expected .zip file')
  })

  it('shows "Expected .md or .markdown" error when a non-md file is selected in md input', async () => {
    renderWithRouter(<HomePage />)

    await fireEvent.change(getMdInput(), {
      target: { files: [createFile('test.txt', 'text/plain')] },
    })

    expect(screen.getByRole('alert')).toHaveTextContent('Expected .md or .markdown file')
  })

  it('clears error when user replaces with a valid file', async () => {
    renderWithRouter(<HomePage />)

    // first pick an invalid file
    await fireEvent.change(getZipInput(), {
      target: { files: [createFile('bad.txt', 'text/plain')] },
    })
    expect(screen.getByRole('alert')).toHaveTextContent('Expected .zip file')

    // then replace with a valid one
    await fireEvent.change(getZipInput(), {
      target: { files: [createFile('good.zip', 'application/zip')] },
    })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('submit button is disabled when extensions are invalid', () => {
    renderWithRouter(<HomePage />)

    // no files selected yet — button should be disabled
    expect(getSubmitButton()).toBeDisabled()
  })

  it('calls navigate to /loading with files when both are valid and submit is clicked', async () => {
    mockNavigate.mockClear()
    renderWithRouter(<HomePage />)

    const zip = createFile('src.zip', 'application/zip')
    const md = createFile('reqs.md', 'text/markdown')

    await fireEvent.change(getZipInput(), {
      target: { files: [zip] },
    })
    await fireEvent.change(getMdInput(), {
      target: { files: [md] },
    })

    expect(getSubmitButton()).not.toBeDisabled()

    await fireEvent.click(getSubmitButton())

    expect(mockNavigate).toHaveBeenCalledWith('/loading', {
      state: { zipFile: zip, mdFile: md },
    })
  })
})
