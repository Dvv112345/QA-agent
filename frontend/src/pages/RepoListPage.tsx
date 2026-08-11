import { useEffect, useState } from 'react'
import PageState from '../components/PageState'
import { Link } from 'react-router-dom'
import { deactivateRepo, fetchRepos } from '../services/api'
import type { RepoResponse } from '../types'
import { formatDate } from '../format'
import './RepoListPage.css'

export default function RepoListPage() {
  const [repos, setRepos] = useState<RepoResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [deactivating, setDeactivating] = useState<number | null>(null)
  const [deactivateError, setDeactivateError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchRepos()
      .then((data) => {
        if (!cancelled) {
          setRepos(data)
          setLoading(false)
        }
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setError(err.message)
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  const handleDeactivate = (repoId: number) => {
    setDeactivating(repoId)
    setDeactivateError(null)
    deactivateRepo(repoId)
      .then(() => fetchRepos())
      .then((data) => setRepos(data))
      .catch((err: Error) => setDeactivateError(err.message))
      .finally(() => setDeactivating(null))
  }

  if (loading) return <PageState kind="loading">Loading repos&hellip;</PageState>
  if (error) return <PageState kind="error">{error}</PageState>

  return (
    <div className="repo-list">
      <nav className="page-nav">
        <Link to="/" className="btn btn-secondary" aria-label="Back to Sprints">
          &larr; Back
        </Link>
      </nav>

      <header className="repo-list-header">
        <h1>Repositories</h1>
      </header>

      {deactivateError && <p className="repo-list-error">{deactivateError}</p>}

      {repos.length === 0 ? (
        <PageState kind="empty">No repos stored yet.</PageState>
      ) : (
        <div className="repo-cards">
          {repos.map((repo) => (
            <div key={repo.id} className="repo-card">
              <div className="repo-card-body">
                <h2>{repo.name}</h2>
                {repo.description && <p className="repo-desc">{repo.description}</p>}
                <a
                  href={repo.github_link}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="repo-link"
                >
                  {repo.github_link}
                </a>
                <time className="repo-date">Added {formatDate(repo.created_at)}</time>
              </div>
              <div className="repo-card-footer">
                <button
                  className="btn btn-small btn-danger"
                  disabled={deactivating === repo.id}
                  onClick={() => handleDeactivate(repo.id)}
                >
                  {deactivating === repo.id ? 'Deactivating…' : 'Deactivate'}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
