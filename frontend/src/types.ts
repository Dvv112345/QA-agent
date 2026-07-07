export interface FileWordCount {
  file: string
  words: number
}

export interface JobStatusResponse {
  job_id: string
  status: string // "queued" | "started" | "finished" | "failed" | "unknown"
  total_files: number
  processed_files: number
  md_result: FileWordCount | null
  zip_results: FileWordCount[] | null
  total_words: number | null
  error: string | null
}

export interface PasswordVerifyRequest {
  password: string
}

export interface AuthCheckResponse {
  valid: boolean
}

export interface UploadResponse {
  job_id: string
  status: string
  zip_filename: string
  markdown_filename: string
  tree: string[]
  tree_text: string
  word_count_enqueued: boolean
  error: string | null
}
