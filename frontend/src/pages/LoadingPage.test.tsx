import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithRouter } from '../test/test-utils'
import LoadingPage from './LoadingPage'

const mockNavigate = vi.fn()

// We mock react-router-dom for useNavigate but keep real useLocation/MemoryRouter for route state.
// For navigation assertions we need the mock, but MemoryRouter from test-utils provides routing context.
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  }
})

// Mock the API layer
vi.mock('../services/api', () => ({
  uploadFiles: vi.fn(),
}))

import { uploadFiles } from '../services/api'

const fakeZip = new File(['zip'], 'test.zip', { type: 'application/zip' })
const fakeMd = new File(['md'], 'test.md', { type: 'text/markdown' })

const mockUploadFiles = uploadFiles as ReturnType<typeof vi.fn>

beforeEach(() => {
  mockNavigate.mockClear()
  mockUploadFiles.mockReset()
})

describe('LoadingPage', () => {
  it('shows fallback message when route state is missing', () => {
    renderWithRouter(<LoadingPage />)
    expect(screen.getByText(/no files to upload/i)).toBeInTheDocument()
    expect(screen.getByText(/go to upload page/i)).toBeInTheDocument()
  })

  it('shows loading spinner while uploadFiles is in-flight', async () => {
    // never resolve — stays loading
    mockUploadFiles.mockReturnValue(new Promise(() => {}))

    renderWithRouter(<LoadingPage />, {
      initialEntries: [{ pathname: '/loading', state: { zipFile: fakeZip, mdFile: fakeMd } }],
    })

    await screen.findByText(/uploading/i)
  })

  it('renders job ID, filenames, and tree_text on successful upload', async () => {
    mockUploadFiles.mockResolvedValue({
      job_id: '20260617-abc123',
      status: 'success',
      zip_filename: 'test.zip',
      markdown_filename: 'test.md',
      tree: ['src/', 'src/main.py'],
      tree_text: 'src/\n└── main.py',
      error: null,
    })

    renderWithRouter(<LoadingPage />, {
      initialEntries: [{ pathname: '/loading', state: { zipFile: fakeZip, mdFile: fakeMd } }],
    })

    await waitFor(() => {
      expect(screen.getByText('Upload Successful')).toBeInTheDocument()
    })

    expect(screen.getByText('20260617-abc123')).toBeInTheDocument()
    expect(screen.getByText('test.zip')).toBeInTheDocument()
    expect(screen.getByText('test.md')).toBeInTheDocument()

    // tree_text in pre block with box-drawing chars
    const pre = document.querySelector('pre.tree-text')
    expect(pre).toBeInTheDocument()
    expect(pre!.textContent).toContain('main.py')
  })

  it('shows error message when uploadFiles rejects', async () => {
    mockUploadFiles.mockRejectedValue(new Error('Network failure'))

    renderWithRouter(<LoadingPage />, {
      initialEntries: [{ pathname: '/loading', state: { zipFile: fakeZip, mdFile: fakeMd } }],
    })

    await waitFor(() => {
      expect(screen.getByText('Upload Failed')).toBeInTheDocument()
    })

    expect(screen.getByText('Network failure')).toBeInTheDocument()
  })

  it('back button navigates to /', () => {
    mockNavigate.mockClear()

    renderWithRouter(<LoadingPage />)

    fireEvent.click(screen.getByText('← Back'))

    expect(mockNavigate).toHaveBeenCalledWith('/')
  })

  it('"Upload Another" button navigates to / on success', async () => {
    mockUploadFiles.mockResolvedValue({
      job_id: '20260617-abc123',
      status: 'success',
      zip_filename: 'test.zip',
      markdown_filename: 'test.md',
      tree: [],
      tree_text: '.',
      error: null,
    })

    renderWithRouter(<LoadingPage />, {
      initialEntries: [{ pathname: '/loading', state: { zipFile: fakeZip, mdFile: fakeMd } }],
    })

    await waitFor(() => {
      expect(screen.getByText('Upload Successful')).toBeInTheDocument()
    })

    // "Upload Another" is a Link — clicking it should work
    const link = screen.getByText('Upload Another')
    expect(link).toBeInTheDocument()
    expect(link.closest('a')).toHaveAttribute('href', '/')
  })
})
