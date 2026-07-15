import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { fetchSprints, finishSprint } from '../services/api'
import type { SprintResponse } from '../types'
import './SprintListPage.css'

export default function SprintListPage() {
  const [sprints, setSprints] = useState<SprintResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [finishing, setFinishing] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchSprints()
      .then((data) => {
        if (!cancelled) {
          setSprints(data)
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

  const handleFinish = (sprintId: number) => {
    setFinishing(sprintId)
    finishSprint(sprintId)
      .then(() => fetchSprints())
      .then((data) => setSprints(data))
      .catch((err: Error) => setError(err.message))
      .finally(() => setFinishing(null))
  }

  if (loading) return <p className="sprint-list-message">Loading sprints&hellip;</p>
  if (error) return <p className="sprint-list-message sprint-list-error">{error}</p>

  return (
    <div className="sprint-list">
      <header className="sprint-list-header">
        <h1>Sprints</h1>
        <div className="sprint-list-actions">
          <Link to="/sprints/new" className="btn btn-primary">
            Create New Sprint
          </Link>
          <Link to="/repos" className="btn btn-secondary">
            Manage Repos
          </Link>
        </div>
      </header>

      {sprints.length === 0 ? (
        <p className="sprint-list-message">
          No sprints yet. <Link to="/sprints/new">Create your first sprint</Link>.
        </p>
      ) : (
        <div className="sprint-cards">
          {sprints.map((sprint) => (
            <div key={sprint.id} className="sprint-card">
              <Link
                to={
                  sprint.active && sprint.has_test_environment_submission
                    ? `/sprints/${sprint.id}/test-environment`
                    : `/sprints/${sprint.id}`
                }
                className="sprint-card-main"
              >
                <h2 className="sprint-card-name">{sprint.name}</h2>
                <p className="sprint-card-repo">{sprint.repo?.name ?? 'Unknown repo'}</p>
                <time className="sprint-card-date">
                  {new Date(sprint.created_at).toLocaleDateString()}
                </time>
              </Link>
              <div className="sprint-card-footer">
                <span className={`badge ${sprint.active ? 'badge-active' : 'badge-finished'}`}>
                  {sprint.active ? 'Active' : 'Finished'}
                </span>
                {sprint.active && (
                  <button
                    className="btn btn-small btn-danger"
                    disabled={finishing === sprint.id}
                    onClick={(e) => {
                      e.preventDefault()
                      handleFinish(sprint.id)
                    }}
                  >
                    {finishing === sprint.id ? 'Finishing…' : 'Finish Sprint'}
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
