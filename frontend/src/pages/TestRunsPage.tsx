import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import ExploratoryCharterModal from '../components/ExploratoryCharterModal'
import RunTestModal from '../components/RunTestModal'
import { fetchExploratoryRuns, fetchSprint, fetchTestRuns } from '../services/api'
import type { ExploratoryRunResponse, SprintResponse, TestRunResponse } from '../types'
import './TestRunsPage.css'

const POLL_INTERVAL_MS = 2500

const STATUS_LABELS: Record<string, string> = {
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
}

const EXPLORATORY_STATUS_LABELS: Record<string, string> = {
  pending: 'Queued',
  running: 'Exploring',
  completed: 'Completed',
  failed: 'Failed',
}

function resultSummary(run: TestRunResponse): string {
  const parts: string[] = []
  if (run.passed_cases > 0) parts.push(`${run.passed_cases} passed`)
  if (run.failed_cases > 0) parts.push(`${run.failed_cases} failed`)
  if (run.error_cases > 0) parts.push(`${run.error_cases} error`)
  return parts.join(' / ')
}

function findingSummary(run: ExploratoryRunResponse): string {
  const parts: string[] = []
  if (run.bug_count > 0) parts.push(`${run.bug_count} bug${run.bug_count === 1 ? '' : 's'}`)
  if (run.issue_count > 0) {
    parts.push(`${run.issue_count} issue${run.issue_count === 1 ? '' : 's'}`)
  }
  if (parts.length === 0 && run.status === 'completed') return 'No findings'
  return parts.join(' / ')
}

export default function TestRunsPage() {
  const { id } = useParams<{ id: string }>()
  const sprintId = Number(id)

  const [sprint, setSprint] = useState<SprintResponse | null>(null)
  const [runs, setRuns] = useState<TestRunResponse[]>([])
  const [exploratoryRuns, setExploratoryRuns] = useState<ExploratoryRunResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [showRunModal, setShowRunModal] = useState(false)
  const [showCharterModal, setShowCharterModal] = useState(false)

  const fetchingRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([fetchSprint(sprintId), fetchTestRuns(sprintId), fetchExploratoryRuns(sprintId)])
      .then(([sprintData, runData, exploratoryData]) => {
        if (!cancelled) {
          setSprint(sprintData)
          setRuns(runData)
          setExploratoryRuns(exploratoryData)
          setLoading(false)
        }
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setLoadError(err.message)
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [sprintId])

  const shouldPoll =
    runs.some((run) => run.status === 'running') ||
    exploratoryRuns.some((run) => run.status === 'pending' || run.status === 'running')

  useEffect(() => {
    if (!shouldPoll) return

    const pollId = setInterval(() => {
      if (fetchingRef.current) return
      fetchingRef.current = true
      Promise.all([fetchTestRuns(sprintId), fetchExploratoryRuns(sprintId)])
        .then(([runData, exploratoryData]) => {
          setRuns(runData)
          setExploratoryRuns(exploratoryData)
        })
        .catch(() => {
          /* transient poll failure — retry on next tick */
        })
        .finally(() => {
          fetchingRef.current = false
        })
    }, POLL_INTERVAL_MS)

    return () => clearInterval(pollId)
  }, [shouldPoll, sprintId])

  if (loading) return <p className="test-runs-message">Loading test runs&hellip;</p>
  if (loadError) return <p className="test-runs-message test-runs-error">{loadError}</p>
  if (!sprint) return <p className="test-runs-message">Sprint not found.</p>

  const active = sprint.active
  // Runs can outlive test_plans_complete becoming false again — guard on
  // absence of runs only, like the other stages' guard shape. Exploration
  // shares the scripted gate, so both lists sit behind it.
  const guarded =
    runs.length === 0 && exploratoryRuns.length === 0 && (!active || !sprint.test_plans_complete)

  return (
    <div className="test-runs">
      <nav className="back-links">
        <Link to="/" className="back-link">
          &larr; Back to Sprints
        </Link>
        <Link to={`/sprints/${sprintId}/test-plans`} className="back-link">
          &larr; Back to Test Plans
        </Link>
      </nav>

      <header className="test-runs-header">
        <h1>Test Runs</h1>
      </header>

      <p className="test-runs-sprint-name">{sprint.name}</p>

      {guarded ? (
        <p className="test-runs-notice">
          {active ? 'Approve every test plan first.' : 'This sprint is finished.'}
        </p>
      ) : (
        <>
          <section className="test-runs-section">
            <div className="test-runs-section-header">
              <h2>Exploratory Sessions</h2>
              {active && (
                <button className="btn btn-primary" onClick={() => setShowCharterModal(true)}>
                  Start exploratory testing
                </button>
              )}
            </div>

            {exploratoryRuns.length === 0 ? (
              <p className="test-runs-empty">No exploratory runs yet.</p>
            ) : (
              <ul className="test-runs-list">
                {exploratoryRuns.map((run) => (
                  <li key={run.id} className="test-run-row">
                    <Link
                      to={`/sprints/${sprintId}/exploratory-runs/${run.id}`}
                      className="test-run-link"
                    >
                      <div className="test-run-row-main">
                        <span className="test-run-requirements">{run.requirement_name}</span>
                        <span className={`run-badge run-badge-${run.status}`}>
                          {EXPLORATORY_STATUS_LABELS[run.status] ?? run.status}
                        </span>
                      </div>
                      <div className="test-run-row-meta">
                        <time>{new Date(run.created_at).toLocaleString()}</time>
                        {findingSummary(run) && <span>{findingSummary(run)}</span>}
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="test-runs-section">
            <div className="test-runs-section-header">
              <h2>Scripted Test Runs</h2>
              {active && (
                <button className="btn btn-primary" onClick={() => setShowRunModal(true)}>
                  Run new test
                </button>
              )}
            </div>

            {runs.length === 0 ? (
              <p className="test-runs-empty">No test runs yet.</p>
            ) : (
              <ul className="test-runs-list">
                {runs.map((run) => (
                  <li key={run.id} className="test-run-row">
                    <Link to={`/sprints/${sprintId}/test-runs/${run.id}`} className="test-run-link">
                      <div className="test-run-row-main">
                        <span className="test-run-requirements">
                          {run.requirement_names.join(', ')}
                        </span>
                        <span className={`run-badge run-badge-${run.status}`}>
                          {STATUS_LABELS[run.status] ?? run.status}
                        </span>
                      </div>
                      <div className="test-run-row-meta">
                        <time>{new Date(run.created_at).toLocaleString()}</time>
                        {resultSummary(run) && <span>{resultSummary(run)}</span>}
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}

      {showRunModal && <RunTestModal sprintId={sprintId} onClose={() => setShowRunModal(false)} />}
      {showCharterModal && (
        <ExploratoryCharterModal sprintId={sprintId} onClose={() => setShowCharterModal(false)} />
      )}
    </div>
  )
}
