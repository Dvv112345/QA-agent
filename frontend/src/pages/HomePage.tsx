import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import './HomePage.css'

export default function HomePage() {
  const navigate = useNavigate()

  const [zipFile, setZipFile] = useState<File | null>(null)
  const [mdFile, setMdFile] = useState<File | null>(null)
  const [zipError, setZipError] = useState<string | null>(null)
  const [mdError, setMdError] = useState<string | null>(null)

  function handleZipChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0] ?? null
    setZipFile(file)
    if (file && !file.name.endsWith('.zip')) {
      setZipError('Expected .zip file')
    } else {
      setZipError(null)
    }
  }

  function handleMdChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0] ?? null
    setMdFile(file)
    if (file && !file.name.endsWith('.md') && !file.name.endsWith('.markdown')) {
      setMdError('Expected .md or .markdown file')
    } else {
      setMdError(null)
    }
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()

    // Validate both files are present and have correct extensions
    let valid = true

    if (!zipFile) {
      setZipError('Please select a .zip file')
      valid = false
    } else if (!zipFile.name.endsWith('.zip')) {
      setZipError('Expected .zip file')
      valid = false
    }

    if (!mdFile) {
      setMdError('Please select a .md or .markdown file')
      valid = false
    } else if (!mdFile.name.endsWith('.md') && !mdFile.name.endsWith('.markdown')) {
      setMdError('Expected .md or .markdown file')
      valid = false
    }

    if (!valid) return

    navigate('/loading', { state: { zipFile, mdFile } })
  }

  const canSubmit = zipFile && mdFile && !zipError && !mdError

  return (
    <main className="home-page">
      <h1>QA Agent Upload</h1>
      <p className="subtitle">
        Upload a source code archive and a requirements document for automated quality assurance
        analysis.
      </p>

      <form className="upload-form" onSubmit={handleSubmit} noValidate>
        <div className="file-field">
          <label htmlFor="zip-input" className="file-label">
            <span className="file-label-text">Source Code (.zip)</span>
            <span className="file-hint">
              {zipFile ? zipFile.name : 'Click to select a .zip file'}
            </span>
          </label>
          <input
            id="zip-input"
            type="file"
            accept=".zip"
            onChange={handleZipChange}
            className="file-input"
          />
          {zipError && (
            <p className="file-error" role="alert">
              {zipError}
            </p>
          )}
        </div>

        <div className="file-field">
          <label htmlFor="md-input" className="file-label">
            <span className="file-label-text">Requirements (.md)</span>
            <span className="file-hint">
              {mdFile ? mdFile.name : 'Click to select a .md or .markdown file'}
            </span>
          </label>
          <input
            id="md-input"
            type="file"
            accept=".md,.markdown"
            onChange={handleMdChange}
            className="file-input"
          />
          {mdError && (
            <p className="file-error" role="alert">
              {mdError}
            </p>
          )}
        </div>

        <button type="submit" className="submit-btn" disabled={!canSubmit}>
          Upload &amp; Analyze
        </button>
      </form>
    </main>
  )
}
