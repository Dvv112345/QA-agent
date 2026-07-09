import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { checkReadmeStatus, createRepo, createSprint, fetchRepos } from '../services/api'
import type { RepoResponse } from '../types'
import './CreateSprintPage.css'

export default function CreateSprintPage() {
  const navigate = useNavigate()

  const [name, setName] = useState('')
  const [repos, setRepos] = useState<RepoResponse[]>([])
  const [selectedRepoId, setSelectedRepoId] = useState<number | null>(null)
  const [hasReadme, setHasReadme] = useState<boolean | null>(null)
  const [readmeFile, setReadmeFile] = useState<File | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reposLoading, setReposLoading] = useState(true)
  const [readmeLoading, setReadmeLoading] = useState(false)

  // Inline repo creation
  const [showNewRepoForm, setShowNewRepoForm] = useState(false)
  const [newRepoUrl, setNewRepoUrl] = useState('')
  const [newRepoToken, setNewRepoToken] = useState('')
  const [creatingRepo, setCreatingRepo] = useState(false)
  const [createRepoError, setCreateRepoError] = useState<string | null>(null)

  useEffect(() => {
    fetchRepos()
      .then(setRepos)
      .catch((err: Error) => setError(err.message))
      .finally(() => setReposLoading(false))
  }, [])

  const handleRepoChange = useCallback((repoId: number) => {
    setSelectedRepoId(repoId)
    setHasReadme(null)
    setReadmeFile(null)
    setReadmeLoading(true)
    checkReadmeStatus(repoId)
      .then((result) => setHasReadme(result.has_readme))
      .catch((err: Error) => setError(err.message))
      .finally(() => setReadmeLoading(false))
  }, [])

  const handleRepoSelect = useCallback(
    (value: string) => {
      if (value === '__new__') {
        setShowNewRepoForm(true)
        setCreateRepoError(null)
        return
      }
      setShowNewRepoForm(false)
      handleRepoChange(Number(value))
    },
    [handleRepoChange],
  )

  const handleCreateRepo = (e: React.FormEvent) => {
    e.preventDefault()
    if (!newRepoUrl.trim()) return

    setCreatingRepo(true)
    setCreateRepoError(null)
    createRepo(newRepoUrl.trim(), newRepoToken || undefined)
      .then((newRepo) => {
        setRepos((prev) => [...prev, newRepo])
        setShowNewRepoForm(false)
        setNewRepoUrl('')
        setNewRepoToken('')
        handleRepoChange(newRepo.id)
      })
      .catch((err: Error) => setCreateRepoError(err.message))
      .finally(() => setCreatingRepo(false))
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim() || selectedRepoId === null) return
    if (hasReadme === false && !readmeFile) return

    setLoading(true)
    setError(null)
    createSprint(name.trim(), selectedRepoId, readmeFile ?? undefined)
      .then(() => navigate('/'))
      .catch((err: Error) => {
        setError(err.message)
        setLoading(false)
      })
  }

  return (
    <div className="create-sprint">
      <h1>Create Sprint</h1>
      <form onSubmit={handleSubmit} className="create-sprint-form">
        <label className="form-field">
          <span>Sprint Name</span>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Sprint 1"
            required
            disabled={loading}
          />
        </label>

        <label className="form-field">
          <span>Repository</span>
          {reposLoading ? (
            <p>Loading repos&hellip;</p>
          ) : showNewRepoForm ? (
            <div className="inline-repo-form">
              <input
                type="url"
                value={newRepoUrl}
                onChange={(e) => setNewRepoUrl(e.target.value)}
                placeholder="https://github.com/owner/repo"
                required
                disabled={creatingRepo}
              />
              <input
                type="password"
                value={newRepoToken}
                onChange={(e) => setNewRepoToken(e.target.value)}
                placeholder="GitHub token (optional, for private repos)"
                disabled={creatingRepo}
              />
              {createRepoError && <p className="form-error">{createRepoError}</p>}
              <div className="form-actions">
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={!newRepoUrl.trim() || creatingRepo}
                  onClick={handleCreateRepo}
                >
                  {creatingRepo ? 'Adding…' : 'Add Repo'}
                </button>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={creatingRepo}
                  onClick={() => {
                    setShowNewRepoForm(false)
                    setCreateRepoError(null)
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <select
              value={selectedRepoId ?? ''}
              onChange={(e) => handleRepoSelect(e.target.value)}
              required
              disabled={loading}
            >
              <option value="" disabled>
                Select a repository
              </option>
              {repos.map((repo) => (
                <option key={repo.id} value={repo.id}>
                  {repo.name}
                </option>
              ))}
              <option value="__new__">+ Create New Repo</option>
            </select>
          )}
        </label>

        {readmeLoading && <p>Checking for README&hellip;</p>}

        {hasReadme === true && (
          <div className="readme-note readme-found">
            <p>README found in repository — it will be downloaded automatically.</p>
            <label className="form-field">
              <span>Or upload your own README to replace it</span>
              <input
                type="file"
                accept=".md,.markdown"
                onChange={(e) => setReadmeFile(e.target.files?.[0] ?? null)}
                disabled={loading}
              />
            </label>
          </div>
        )}

        {hasReadme === false && (
          <div className="readme-note readme-required">
            <p>This repository has no README. Please upload one.</p>
            <label className="form-field">
              <span>README file (required)</span>
              <input
                type="file"
                accept=".md,.markdown"
                required
                onChange={(e) => setReadmeFile(e.target.files?.[0] ?? null)}
                disabled={loading}
              />
            </label>
          </div>
        )}

        {error && <p className="form-error">{error}</p>}

        <div className="form-actions">
          <button
            type="submit"
            className="btn btn-primary"
            disabled={loading || hasReadme === false ? !readmeFile : false}
          >
            {loading ? 'Creating…' : 'Create Sprint'}
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => navigate('/')}
            disabled={loading}
          >
            Cancel
          </button>
        </div>
      </form>
    </div>
  )
}
