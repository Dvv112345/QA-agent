import { describe, it, expect, vi } from 'vitest'
import { uploadFiles } from './api'
import type { UploadResponse } from '../types'

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
  error: null,
}

describe('uploadFiles', () => {
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
