import { useEffect, useState } from 'react'
import PageState from '../components/PageState'
import { Link } from 'react-router-dom'
import FinishSprintModal from '../components/FinishSprintModal'
import { fetchSprints, finishSprint } from '../services/api'
import type { SprintResponse } from '../types'
import { formatDate } from '../format'
import './SprintListPage.css'

export default function SprintListPage() {
  const [sprints, setSprints] = useState<SprintResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [finishing, setFinishing] = useState<number | null>(null)
  const [confirmingFinish, setConfirmingFinish] = useState<SprintResponse | null>(null)
  // Kept apart from `error`, which means "the list would not load" and blanks
  // the page. A finish that is refused should report inside the dialog the user
  // is looking at, not destroy the list behind it.
  const [finishError, setFinishError] = useState<string | null>(null)

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
    setFinishError(null)
    finishSprint(sprintId)
      // The response is the updated sprint — every other caller uses it.
      // Refetching the list instead re-downloaded every sprint to learn
      // what this one call already returned.
      .then((updated) => {
        setSprints((prev) => prev.map((s) => (s.id === updated.id ? updated : s)))
        setConfirmingFinish(null)
      })
      .catch((err: Error) => setFinishError(err.message))
      .finally(() => setFinishing(null))
  }

  if (loading) return <PageState kind="loading">Loading sprints&hellip;</PageState>
  if (error) return <PageState kind="error">{error}</PageState>

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
        <PageState kind="empty">
          No sprints yet. <Link to="/sprints/new">Create your first sprint</Link>.
        </PageState>
      ) : (
        <div className="sprint-cards">
          {sprints.map((sprint) => (
            <div key={sprint.id} className="sprint-card">
              <Link
                to={
                  // All three run modes share the test-runs page, so any
                  // one of them is enough to deep-link there.
                  sprint.active &&
                  (sprint.has_test_runs ||
                    sprint.has_exploratory_runs ||
                    sprint.has_nonfunctional_runs)
                    ? `/sprints/${sprint.id}/test-runs`
                    : sprint.active && sprint.has_test_plans
                      ? `/sprints/${sprint.id}/test-plans`
                      : sprint.active && sprint.has_test_environment_submission
                        ? `/sprints/${sprint.id}/test-environment`
                        : `/sprints/${sprint.id}`
                }
                className="sprint-card-main"
              >
                <h2 className="sprint-card-name">{sprint.name}</h2>
                <p className="sprint-card-repo">{sprint.repo?.name ?? 'Unknown repo'}</p>
                <time className="sprint-card-date">{formatDate(sprint.created_at)}</time>
              </Link>
              <div className="sprint-card-footer">
                <span className={`badge ${sprint.active ? 'badge-active' : 'badge-finished'}`}>
                  {sprint.active ? 'Active' : 'Finished'}
                </span>
                {sprint.active && (
                  <button
                    className="btn btn-small btn-danger"
                    disabled={finishing === sprint.id}
                    onClick={() => setConfirmingFinish(sprint)}
                  >
                    Finish Sprint
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {confirmingFinish && (
        <FinishSprintModal
          sprintName={confirmingFinish.name}
          busy={finishing === confirmingFinish.id}
          error={finishError}
          onConfirm={() => handleFinish(confirmingFinish.id)}
          onCancel={() => {
            setConfirmingFinish(null)
            setFinishError(null)
          }}
        />
      )}
    </div>
  )
}
