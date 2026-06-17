import type { UploadResponse } from '../types'

const API_BASE = 'http://localhost:8000'

export async function uploadFiles(zipFile: File, mdFile: File): Promise<UploadResponse> {
  const formData = new FormData()
  formData.append('zip_file', zipFile)
  formData.append('markdown_file', mdFile)

  const response = await fetch(`${API_BASE}/api/upload`, {
    method: 'POST',
    body: formData,
  })

  if (!response.ok) {
    let message = `Upload failed (${response.status})`
    try {
      const body = await response.json()
      if (body.detail) {
        if (typeof body.detail === 'string') {
          message = body.detail
        } else if (Array.isArray(body.detail)) {
          message = body.detail.map((e: { msg: string }) => e.msg).join('; ')
        }
      }
    } catch {
      // response body wasn't JSON — keep the default message
    }
    throw new Error(message)
  }

  return response.json() as Promise<UploadResponse>
}
