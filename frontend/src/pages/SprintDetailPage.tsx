import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { fetchSprint, finishSprint } from '../services/api'
import type { SprintResponse } from '../types'
import './SprintDetailPage.css'

export default function SprintDetailPage() {
  const { id } = useParams<{ id: string }>()
  const sprintId = Number(id)

  const [sprint, setSprint] = useState<SprintResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [finishing, setFinishing] = useState(false)

  useEffect(() => {
    let cancelled = false
    fetchSprint(sprintId)
      .then((data) => {
        if (!cancelled) {
          setSprint(data)
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
  }, [sprintId])

  const handleFinish = () => {
    setFinishing(true)
    finishSprint(sprintId)
      .then(setSprint)
      .catch((err: Error) => setError(err.message))
      .finally(() => setFinishing(false))
  }

  if (loading) return <p className="sprint-detail-message">Loading sprint&hellip;</p>
  if (error) return <p className="sprint-detail-message sprint-detail-error">{error}</p>
  if (!sprint) return <p className="sprint-detail-message">Sprint not found.</p>

  const repo = sprint.repo

  return (
    <div className="sprint-detail">
      <Link to="/" className="back-link">
        &larr; Back to Sprints
      </Link>

      <header className="sprint-detail-header">
        <h1>{sprint.name}</h1>
        <span className={`badge ${sprint.active ? 'badge-active' : 'badge-finished'}`}>
          {sprint.active ? 'Active' : 'Finished'}
        </span>
      </header>

      <time className="sprint-detail-date">
        Created {new Date(sprint.created_at).toLocaleDateString()}
      </time>

      {repo && (
        <section className="repo-info-card">
          <h2>Repository</h2>
          <dl>
            <dt>Name</dt>
            <dd>{repo.name}</dd>
            {repo.description && (
              <>
                <dt>Description</dt>
                <dd>{repo.description}</dd>
              </>
            )}
            <dt>GitHub</dt>
            <dd>
              <a href={repo.github_link} target="_blank" rel="noopener noreferrer">
                {repo.github_link}
              </a>
            </dd>
          </dl>
        </section>
      )}

      {sprint.active && (
        <button className="btn btn-danger" disabled={finishing} onClick={handleFinish}>
          {finishing ? 'Finishing…' : 'Finish Sprint'}
        </button>
      )}
    </div>
  )
}
