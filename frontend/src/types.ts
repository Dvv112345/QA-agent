export interface UploadResponse {
  job_id: string
  status: string
  zip_filename: string
  markdown_filename: string
  tree: string[]
  tree_text: string
  error: string | null
}
