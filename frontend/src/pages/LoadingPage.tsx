import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, Link } from 'react-router-dom'
import WordCountProgress from '../components/WordCountProgress'
import WordCountResult from '../components/WordCountResult'
import { fetchJobStatus, uploadFiles } from '../services/api'
import type { JobStatusResponse, UploadResponse } from '../types'
import './LoadingPage.css'

interface LocationState {
  zipFile?: File
  mdFile?: File
}

// ── Fallback used when the status endpoint itself fails ────────────────────
function emptyJobStatus(jobId: string): JobStatusResponse {
  return {
    job_id: jobId,
    status: 'unknown',
    total_files: 0,
    processed_files: 0,
    md_result: null,
    zip_results: null,
    total_words: null,
    error: null,
  }
}

// ── Terminal job statuses (polling should stop) ────────────────────────────
const TERMINAL_STATUSES = new Set(['finished', 'failed', 'unknown'])

export default function LoadingPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const state = location.state as LocationState | null

  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading')
  const [result, setResult] = useState<UploadResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [retryKey, setRetryKey] = useState(0)
  const [jobStatus, setJobStatus] = useState<JobStatusResponse | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const fetchingRef = useRef(false)

  // ── Upload effect ────────────────────────────────────────────────────
  useEffect(() => {
    if (!state?.zipFile || !state?.mdFile) {
      return
    }

    let cancelled = false

    async function doUpload() {
      try {
        const data = await uploadFiles(state!.zipFile!, state!.mdFile!)
        if (!cancelled) {
          setResult(data)
          setStatus('success')
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Upload failed')
          setStatus('error')
        }
      }
    }

    doUpload()

    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state?.zipFile, state?.mdFile, retryKey])

  // ── Polling effect ───────────────────────────────────────────────────
  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  useEffect(() => {
    if (status !== 'success' || !result?.word_count_enqueued) {
      return
    }

    const jobId = result.job_id

    function pollOnce() {
      if (fetchingRef.current) return
      fetchingRef.current = true

      fetchJobStatus(jobId)
        .then((data) => {
          setJobStatus(data)
          if (TERMINAL_STATUSES.has(data.status)) {
            stopPolling()
          }
        })
        .catch(() => {
          setJobStatus((prev) => prev ?? emptyJobStatus(jobId))
          stopPolling()
        })
        .finally(() => {
          fetchingRef.current = false
        })
    }

    pollOnce()
    pollRef.current = setInterval(pollOnce, 5000)

    return () => {
      stopPolling()
      fetchingRef.current = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, result?.word_count_enqueued, result?.job_id])

  // ── Missing state ────────────────────────────────────────────────────
  if (!state?.zipFile || !state?.mdFile) {
    return (
      <main className="loading-page">
        <button type="button" className="back-btn" onClick={() => navigate('/')}>
          ← Back
        </button>
        <div className="loading-missing">
          <p>No files to upload.</p>
          <Link to="/">Go to upload page</Link>
        </div>
      </main>
    )
  }

  const isJobTerminal = jobStatus !== null && TERMINAL_STATUSES.has(jobStatus.status)

  return (
    <main className="loading-page">
      <button type="button" className="back-btn" onClick={() => navigate('/')}>
        ← Back
      </button>

      {status === 'loading' && (
        <div className="loading-spinner-container">
          <div className="spinner" />
          <p className="loading-text">Uploading…</p>
        </div>
      )}

      {status === 'success' && result && (
        <div className="result-container">
          <h2>Upload Successful</h2>
          <dl className="result-meta">
            <dt>Job ID</dt>
            <dd>{result.job_id}</dd>

            <dt>Zip file</dt>
            <dd>{result.zip_filename}</dd>

            <dt>Markdown file</dt>
            <dd>{result.markdown_filename}</dd>
          </dl>

          <h3>Directory Tree</h3>
          <pre className="tree-text">{result.tree_text}</pre>

          {result.word_count_enqueued && (
            <div className="progress-section">
              <h3>Word Count Analysis</h3>

              {!isJobTerminal && jobStatus && (
                <div className="progress-container">
                  <WordCountProgress jobStatus={jobStatus} />
                </div>
              )}

              {jobStatus && <WordCountResult jobStatus={jobStatus} />}
            </div>
          )}

          <Link to="/" className="upload-another-btn">
            Upload Another
          </Link>
        </div>
      )}

      {status === 'error' && (
        <div className="error-container">
          <h2>Upload Failed</h2>
          <p className="error-message">{error}</p>
          <div className="error-actions">
            <button
              type="button"
              className="retry-btn"
              onClick={() => {
                setStatus('loading')
                setError(null)
                setResult(null)
                setJobStatus(null)
                setRetryKey((k) => k + 1)
              }}
            >
              Retry
            </button>
            <Link to="/" className="back-link">
              Back to Upload
            </Link>
          </div>
        </div>
      )}
    </main>
  )
}
