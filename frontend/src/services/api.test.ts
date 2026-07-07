import { describe, it, expect, vi, beforeEach } from 'vitest'
import { checkAuthStatus, fetchJobStatus, uploadFiles, verifyPassword } from './api'
import type { JobStatusResponse, UploadResponse } from '../types'

function mockFetch(response: Response) {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(response)
}

const fakeZip = new File(['zip content'], 'test.zip', { type: 'application/zip' })
const fakeMd = new File(['# Hello'], 'test.md', { type: 'text/markdown' })

const successResponse: UploadResponse = {
  job_id: '20260617-120000-abc123',
  status: 'success',
  zip_filename: 'test.zip',
  markdown_filename: 'test.md',
  tree: ['test/', 'test/main.py'],
  tree_text: 'test/\n└── main.py',
  word_count_enqueued: false,
  error: null,
}

describe('uploadFiles', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('returns parsed UploadResponse on 200 with valid JSON', async () => {
    mockFetch(new Response(JSON.stringify(successResponse), { status: 200 }))

    const result = await uploadFiles(fakeZip, fakeMd)
    expect(result).toEqual(successResponse)
  })

  it('throws with backend error message on 422 response', async () => {
    mockFetch(new Response(JSON.stringify({ detail: 'Invalid file type' }), { status: 422 }))

    await expect(uploadFiles(fakeZip, fakeMd)).rejects.toThrow('Invalid file type')
  })

  it('throws on network failure (fetch rejects)', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('Network error'))

    await expect(uploadFiles(fakeZip, fakeMd)).rejects.toThrow('Network error')
  })

  it('sends correct FormData fields (zip_file, markdown_file)', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify(successResponse), { status: 200 }))

    await uploadFiles(fakeZip, fakeMd)

    const [, options] = fetchSpy.mock.calls[0]
    const body = options?.body as FormData

    expect(body.get('zip_file')).toBe(fakeZip)
    expect(body.get('markdown_file')).toBe(fakeMd)
  })
})

describe('fetchJobStatus', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('returns JobStatusResponse on 200', async () => {
    const jobData: JobStatusResponse = {
      job_id: 'job-1',
      status: 'finished',
      total_files: 3,
      processed_files: 3,
      md_result: { file: 'requirements.md', words: 42 },
      zip_results: [{ file: 'main.py', words: 10 }],
      total_words: 52,
      error: null,
    }
    mockFetch(new Response(JSON.stringify(jobData), { status: 200 }))

    const result = await fetchJobStatus('job-1')
    expect(result).toEqual(jobData)
    expect(result.total_words).toBe(52)
  })

  it('throws on non-200 response', async () => {
    mockFetch(new Response('Not found', { status: 404 }))

    await expect(fetchJobStatus('bad-id')).rejects.toThrow('Job status request failed (404)')
  })

  it('calls the correct endpoint URL', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          job_id: 'job-2',
          status: 'queued',
          total_files: 0,
          processed_files: 0,
          md_result: null,
          zip_results: null,
          total_words: null,
          error: null,
        }),
        { status: 200 },
      ),
    )

    await fetchJobStatus('job-2')
    const [url] = fetchSpy.mock.calls[0]
    expect(url).toContain('/api/jobs/job-2/status')
  })
})

describe('checkAuthStatus', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('returns AuthCheckResponse with valid=true on 200', async () => {
    mockFetch(new Response(JSON.stringify({ valid: true }), { status: 200 }))

    const result = await checkAuthStatus()
    expect(result).toEqual({ valid: true })
  })

  it('returns AuthCheckResponse with valid=false on 200', async () => {
    mockFetch(new Response(JSON.stringify({ valid: false }), { status: 200 }))

    const result = await checkAuthStatus()
    expect(result).toEqual({ valid: false })
  })

  it('throws on non-200 response', async () => {
    mockFetch(new Response('Internal error', { status: 500 }))

    await expect(checkAuthStatus()).rejects.toThrow('Auth check failed (500)')
  })
})

describe('verifyPassword', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('returns AuthCheckResponse with valid=true on match', async () => {
    mockFetch(new Response(JSON.stringify({ valid: true }), { status: 200 }))

    const result = await verifyPassword('secret123')
    expect(result).toEqual({ valid: true })
  })

  it('returns AuthCheckResponse with valid=false on mismatch', async () => {
    mockFetch(new Response(JSON.stringify({ valid: false }), { status: 200 }))

    const result = await verifyPassword('wrong')
    expect(result).toEqual({ valid: false })
  })

  it('sends POST with correct JSON body', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ valid: true }), { status: 200 }))

    await verifyPassword('secret123')

    const [, options] = fetchSpy.mock.calls[0]
    expect(options?.method).toBe('POST')
    expect(options?.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(options?.body).toBe(JSON.stringify({ password: 'secret123' }))
  })

  it('throws with detail message on error response', async () => {
    mockFetch(new Response(JSON.stringify({ detail: 'Invalid password' }), { status: 422 }))

    await expect(verifyPassword('bad')).rejects.toThrow('Invalid password')
  })
})
