import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithRouter } from '../test/test-utils'
import LoadingPage from './LoadingPage'

const mockNavigate = vi.fn()

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
  fetchJobStatus: vi.fn(),
}))

import { fetchJobStatus, uploadFiles } from '../services/api'

const fakeZip = new File(['zip'], 'test.zip', { type: 'application/zip' })
const fakeMd = new File(['md'], 'test.md', { type: 'text/markdown' })

const mockUploadFiles = uploadFiles as ReturnType<typeof vi.fn>
const mockFetchJobStatus = fetchJobStatus as ReturnType<typeof vi.fn>

beforeEach(() => {
  mockNavigate.mockClear()
  mockUploadFiles.mockReset()
  mockFetchJobStatus.mockReset()
})

describe('LoadingPage', () => {
  it('shows fallback message when route state is missing', () => {
    renderWithRouter(<LoadingPage />)
    expect(screen.getByText(/no files to upload/i)).toBeInTheDocument()
    expect(screen.getByText(/go to upload page/i)).toBeInTheDocument()
  })

  it('shows loading spinner while uploadFiles is in-flight', async () => {
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
      word_count_enqueued: false,
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

    const pre = document.querySelector('pre.tree-text')
    expect(pre).toBeInTheDocument()
    expect(pre!.textContent).toContain('main.py')
  })

  it('does not show progress section when word_count_enqueued is false', async () => {
    mockUploadFiles.mockResolvedValue({
      job_id: '20260617-noprogress',
      status: 'success',
      zip_filename: 'test.zip',
      markdown_filename: 'test.md',
      tree: [],
      tree_text: '.',
      word_count_enqueued: false,
      error: null,
    })

    renderWithRouter(<LoadingPage />, {
      initialEntries: [{ pathname: '/loading', state: { zipFile: fakeZip, mdFile: fakeMd } }],
    })

    await waitFor(() => {
      expect(screen.getByText('Upload Successful')).toBeInTheDocument()
    })

    expect(screen.queryByText('Word Count Analysis')).not.toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
  })

  it('starts polling for job status when word_count_enqueued is true', async () => {
    mockUploadFiles.mockResolvedValue({
      job_id: 'poll-job-1',
      status: 'success',
      zip_filename: 'test.zip',
      markdown_filename: 'test.md',
      tree: [],
      tree_text: '.',
      word_count_enqueued: true,
      error: null,
    })

    mockFetchJobStatus.mockResolvedValueOnce({
      job_id: 'poll-job-1',
      status: 'started',
      total_files: 5,
      processed_files: 2,
      md_result: null,
      zip_results: null,
      total_words: null,
      error: null,
    })

    renderWithRouter(<LoadingPage />, {
      initialEntries: [{ pathname: '/loading', state: { zipFile: fakeZip, mdFile: fakeMd } }],
    })

    await waitFor(() => {
      expect(screen.getByText('Upload Successful')).toBeInTheDocument()
    })

    await waitFor(() => {
      expect(screen.getByText('Word Count Analysis')).toBeInTheDocument()
    })

    expect(screen.getByRole('progressbar')).toBeInTheDocument()
    expect(screen.getByText(/Processing files: 2 \/ 5/)).toBeInTheDocument()
  })

  it('stops polling and shows results when job finishes', async () => {
    mockUploadFiles.mockResolvedValue({
      job_id: 'finish-job',
      status: 'success',
      zip_filename: 'test.zip',
      markdown_filename: 'test.md',
      tree: [],
      tree_text: '.',
      word_count_enqueued: true,
      error: null,
    })

    mockFetchJobStatus.mockResolvedValue({
      job_id: 'finish-job',
      status: 'finished',
      total_files: 1,
      processed_files: 1,
      md_result: { file: 'requirements.md', words: 100 },
      zip_results: [{ file: 'main.py', words: 50 }],
      total_words: 150,
      error: null,
    })

    renderWithRouter(<LoadingPage />, {
      initialEntries: [{ pathname: '/loading', state: { zipFile: fakeZip, mdFile: fakeMd } }],
    })

    await waitFor(() => {
      expect(screen.getByText('Requirements Document')).toBeInTheDocument()
    })

    expect(screen.getByText('100')).toBeInTheDocument()
    expect(screen.getByText('50')).toBeInTheDocument()
    expect(screen.getByText('150')).toBeInTheDocument()
    expect(screen.getByText('Source Files')).toBeInTheDocument()

    // When status is "finished", polling stops — so fetchJobStatus should
    // have been called exactly once (the initial immediate fetch).
    // The interval should have been cleared because status is terminal.
    expect(mockFetchJobStatus).toHaveBeenCalledTimes(1)
  })

  it('shows error message when job fails', async () => {
    mockUploadFiles.mockResolvedValue({
      job_id: 'fail-job',
      status: 'success',
      zip_filename: 'test.zip',
      markdown_filename: 'test.md',
      tree: [],
      tree_text: '.',
      word_count_enqueued: true,
      error: null,
    })

    mockFetchJobStatus.mockResolvedValue({
      job_id: 'fail-job',
      status: 'failed',
      total_files: 5,
      processed_files: 2,
      md_result: null,
      zip_results: null,
      total_words: null,
      error: 'File missing during processing: /tmp/bad.py',
    })

    renderWithRouter(<LoadingPage />, {
      initialEntries: [{ pathname: '/loading', state: { zipFile: fakeZip, mdFile: fakeMd } }],
    })

    await waitFor(() => {
      expect(screen.getByText('Processing failed')).toBeInTheDocument()
    })

    expect(screen.getByText(/bad\.py/)).toBeInTheDocument()
  })

  it('shows "Waiting for worker" when job is queued with no progress', async () => {
    mockUploadFiles.mockResolvedValue({
      job_id: 'queued-job',
      status: 'success',
      zip_filename: 'test.zip',
      markdown_filename: 'test.md',
      tree: [],
      tree_text: '.',
      word_count_enqueued: true,
      error: null,
    })

    mockFetchJobStatus.mockResolvedValue({
      job_id: 'queued-job',
      status: 'queued',
      total_files: 10,
      processed_files: 0,
      md_result: null,
      zip_results: null,
      total_words: null,
      error: null,
    })

    renderWithRouter(<LoadingPage />, {
      initialEntries: [{ pathname: '/loading', state: { zipFile: fakeZip, mdFile: fakeMd } }],
    })

    await waitFor(() => {
      expect(screen.getByText('Waiting for worker…')).toBeInTheDocument()
    })
  })

  it('cleans up polling interval on unmount', async () => {
    mockUploadFiles.mockResolvedValue({
      job_id: 'cleanup-job',
      status: 'success',
      zip_filename: 'test.zip',
      markdown_filename: 'test.md',
      tree: [],
      tree_text: '.',
      word_count_enqueued: true,
      error: null,
    })

    // Return "started" so polling continues (not terminal)
    mockFetchJobStatus.mockResolvedValue({
      job_id: 'cleanup-job',
      status: 'started',
      total_files: 10,
      processed_files: 0,
      md_result: null,
      zip_results: null,
      total_words: null,
      error: null,
    })

    const { unmount } = renderWithRouter(<LoadingPage />, {
      initialEntries: [{ pathname: '/loading', state: { zipFile: fakeZip, mdFile: fakeMd } }],
    })

    await waitFor(() => {
      expect(screen.getByText('Word Count Analysis')).toBeInTheDocument()
    })

    const callCount = mockFetchJobStatus.mock.calls.length
    unmount()

    // After unmount, the interval is cleared — no more calls to fetchJobStatus
    // waitFor polls internally; after a few retries, the call count should still be unchanged
    // Give it a moment for any straggling async work
    await new Promise((r) => setTimeout(r, 100))
    expect(mockFetchJobStatus).toHaveBeenCalledTimes(callCount)
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
      word_count_enqueued: false,
      error: null,
    })

    renderWithRouter(<LoadingPage />, {
      initialEntries: [{ pathname: '/loading', state: { zipFile: fakeZip, mdFile: fakeMd } }],
    })

    await waitFor(() => {
      expect(screen.getByText('Upload Successful')).toBeInTheDocument()
    })

    const link = screen.getByText('Upload Another')
    expect(link).toBeInTheDocument()
    expect(link.closest('a')).toHaveAttribute('href', '/')
  })
})
