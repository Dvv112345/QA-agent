import { useEffect, useState } from 'react'
import { useLocation, useNavigate, Link } from 'react-router-dom'
import { uploadFiles } from '../services/api'
import type { UploadResponse } from '../types'
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

  useEffect(() => {
    if (!state?.zipFile || !state?.mdFile) {
      // Files missing — don't attempt upload
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
  }, [state])

  // Missing state: user navigated directly to /loading
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
                // Re-trigger useEffect by forcing a re-render;
                // the effect captures state which hasn't changed,
                // so we use a remount trick via a key in a parent
                // — simplest: just re-run upload.
                uploadFiles(state.zipFile!, state.mdFile!)
                  .then((data) => {
                    setResult(data)
                    setStatus('success')
                  })
                  .catch((err) => {
                    setError(err instanceof Error ? err.message : 'Upload failed')
                    setStatus('error')
                  })
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
