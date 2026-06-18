import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, Link } from 'react-router-dom'
import { fetchJobStatus, uploadFiles } from '../services/api'
import type { JobStatusResponse, UploadResponse } from '../types'
import './LoadingPage.css'

interface LocationState {
  zipFile?: File
  mdFile?: File
}

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
    // Only poll when upload succeeded AND a word-count job was enqueued
    if (status !== 'success' || !result?.word_count_enqueued) {
      return
    }

    const jobId = result.job_id

    // Fetch immediately, then every 5 seconds
    fetchJobStatus(jobId)
      .then((data) => {
        setJobStatus(data)
        if (data.status === 'finished' || data.status === 'failed' || data.status === 'unknown') {
          stopPolling()
        }
      })
      .catch(() => {
        // If the status endpoint itself fails, treat as unknown and stop
        setJobStatus(
          (prev) =>
            prev ?? {
              job_id: jobId,
              status: 'unknown',
              total_files: 0,
              processed_files: 0,
              md_result: null,
              zip_results: null,
              total_words: null,
              error: null,
            },
        )
        stopPolling()
      })

    pollRef.current = setInterval(() => {
      fetchJobStatus(jobId)
        .then((data) => {
          setJobStatus(data)
          if (data.status === 'finished' || data.status === 'failed' || data.status === 'unknown') {
            stopPolling()
          }
        })
        .catch(() => {
          setJobStatus(
            (prev) =>
              prev ?? {
                job_id: jobId,
                status: 'unknown',
                total_files: 0,
                processed_files: 0,
                md_result: null,
                zip_results: null,
                total_words: null,
                error: null,
              },
          )
          stopPolling()
        })
    }, 5000)

    return () => {
      stopPolling()
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

  // ── Compute percentage on the frontend ───────────────────────────────
  const pct =
    jobStatus && jobStatus.total_files > 0
      ? Math.round((jobStatus.processed_files / jobStatus.total_files) * 100)
      : 0

  const isJobTerminal =
    jobStatus &&
    (jobStatus.status === 'finished' ||
      jobStatus.status === 'failed' ||
      jobStatus.status === 'unknown')

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

          {/* ── Word-count progress section (only when job was enqueued) ── */}
          {result.word_count_enqueued && (
            <div className="progress-section">
              <h3>Word Count Analysis</h3>

              {!isJobTerminal && (
                <div className="progress-container">
                  {jobStatus && jobStatus.status === 'queued' && !jobStatus.processed_files ? (
                    <div className="progress-waiting">
                      <div className="waiting-pulse" />
                      <p>Waiting for worker…</p>
                    </div>
                  ) : (
                    <>
                      <div className="progress-bar-bg">
                        <div
                          className="progress-bar-fill"
                          style={{ width: `${pct}%` }}
                          role="progressbar"
                          aria-valuenow={pct}
                          aria-valuemin={0}
                          aria-valuemax={100}
                        />
                      </div>
                      <p className="progress-text">
                        Processing files: {jobStatus?.processed_files ?? 0} /{' '}
                        {jobStatus?.total_files ?? 0} ({pct}%)
                      </p>
                    </>
                  )}
                </div>
              )}

              {/* Finished — show results */}
              {jobStatus?.status === 'finished' && jobStatus.md_result && jobStatus.zip_results && (
                <div className="results-container">
                  <div className="results-section">
                    <h4>Requirements Document</h4>
                    <table className="results-table">
                      <thead>
                        <tr>
                          <th>File</th>
                          <th>Words</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr>
                          <td>{jobStatus.md_result.file}</td>
                          <td>{jobStatus.md_result.words.toLocaleString()}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>

                  <div className="results-section">
                    <h4>Source Files</h4>
                    <table className="results-table">
                      <thead>
                        <tr>
                          <th>File</th>
                          <th>Words</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...jobStatus.zip_results]
                          .sort((a, b) => a.file.localeCompare(b.file))
                          .map((f) => (
                            <tr key={f.file}>
                              <td className="file-cell">{f.file}</td>
                              <td>{f.words.toLocaleString()}</td>
                            </tr>
                          ))}
                      </tbody>
                    </table>
                  </div>

                  <div className="results-summary">
                    <span>Total words</span>
                    <span>{jobStatus.total_words?.toLocaleString() ?? 0}</span>
                  </div>
                </div>
              )}

              {/* Failed */}
              {jobStatus?.status === 'failed' && (
                <div className="job-error">
                  <p className="job-error-title">Processing failed</p>
                  <p className="job-error-detail">{jobStatus.error ?? 'Unknown error'}</p>
                </div>
              )}

              {/* Unknown (expired or never submitted) */}
              {jobStatus?.status === 'unknown' && (
                <div className="job-error">
                  <p className="job-error-title">Job status unavailable</p>
                  <p className="job-error-detail">
                    The job result may have expired or the worker is not running.
                  </p>
                </div>
              )}
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
