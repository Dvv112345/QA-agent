import { useRef, useState } from 'react'
import { uploadPrd } from '../services/api'
import type { RequirementResponse } from '../types'
import './PrdUploadForm.css'

interface Props {
  sprintId: number
  /** PRD-derived requirements already exist — a new upload replaces them. */
  hasPrdRequirements: boolean
  onUploaded: (created: RequirementResponse[]) => void
}

export default function PrdUploadForm({ sprintId, hasPrdRequirements, onUploaded }: Props) {
  const [file, setFile] = useState<File | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const doUpload = () => {
    if (!file) return
    setConfirming(false)
    setUploading(true)
    setError(null)
    uploadPrd(sprintId, file)
      .then((created) => {
        setFile(null)
        if (inputRef.current) inputRef.current.value = ''
        onUploaded(created)
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setUploading(false))
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!file) return
    if (hasPrdRequirements) {
      setConfirming(true)
      return
    }
    doUpload()
  }

  return (
    <form className="prd-upload-form" onSubmit={handleSubmit}>
      <h3>Upload a PRD</h3>
      <p className="prd-upload-hint">
        Upload a product requirements document (.md, .txt, .pdf, .docx) to split it into
        requirements automatically. You can still add requirements manually below.
      </p>

      <div className="prd-upload-controls">
        <input
          ref={inputRef}
          type="file"
          accept=".md,.markdown,.txt,.pdf,.docx"
          aria-label="PRD file"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null)
            setConfirming(false)
          }}
          disabled={uploading}
        />
        <button type="submit" className="btn btn-primary" disabled={uploading || !file}>
          {uploading ? 'Splitting PRD into requirements…' : 'Upload PRD'}
        </button>
      </div>

      {confirming && (
        <div className="prd-upload-confirm">
          <p>
            This sprint already has requirements from a previous PRD upload. Uploading a new PRD
            replaces them — manually added requirements are kept. Continue?
          </p>
          <div className="prd-upload-confirm-actions">
            <button
              type="button"
              className="btn btn-danger"
              onClick={doUpload}
              disabled={uploading}
            >
              Replace requirements
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setConfirming(false)}
              disabled={uploading}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && <p className="prd-upload-error">{error}</p>}
    </form>
  )
}
