import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import TestCaseExecutionRow from '../components/TestCaseExecutionRow'
import { fetchSprint, fetchTestRun, restartTestExecution } from '../services/api'
import type { SprintResponse, TestExecutionResponse, TestRunDetailResponse } from '../types'
import './TestRunDetailPage.css'

const POLL_INTERVAL_MS = 2500

const STATUS_LABELS: Record<string, string> = {
  pending: 'Queued',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
}

export default function TestRunDetailPage() {
  const { id, runId } = useParams<{ id: string; runId: string }>()
  const sprintId = Number(id)
  const runIdNum = Number(runId)

  const [sprint, setSprint] = useState<SprintResponse | null>(null)
  const [run, setRun] = useState<TestRunDetailResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [restarting, setRestarting] = useState<number | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const fetchingRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([fetchSprint(sprintId), fetchTestRun(runIdNum)])
      .then(([sprintData, runData]) => {
        if (!cancelled) {
          setSprint(sprintData)
          setRun(runData)
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
  }, [sprintId, runIdNum])

  const shouldPoll = run?.status === 'running'

  useEffect(() => {
    if (!shouldPoll) return

    const pollId = setInterval(() => {
      if (fetchingRef.current) return
      fetchingRef.current = true
      fetchTestRun(runIdNum)
        .then(setRun)
        .catch(() => {
          /* transient poll failure — retry on next tick */
        })
        .finally(() => {
          fetchingRef.current = false
        })
    }, POLL_INTERVAL_MS)

    return () => clearInterval(pollId)
  }, [shouldPoll, runIdNum])

  const handleRestart = (execution: TestExecutionResponse) => {
    setRestarting(execution.id)
    setActionError(null)
    restartTestExecution(execution.id)
      .then((updated) => {
        setRun((prev) =>
          prev
            ? {
                ...prev,
                executions: prev.executions.map((e) =>
                  e.id === updated.id ? { ...e, ...updated } : e,
                ),
              }
            : prev,
        )
      })
      .catch((err: Error) => setActionError(err.message))
      .finally(() => setRestarting(null))
  }

  if (loading) return <p className="test-run-detail-message">Loading test run&hellip;</p>
  if (loadError) return <p className="test-run-detail-message test-run-detail-error">{loadError}</p>
  if (!run || !sprint) return <p className="test-run-detail-message">Test run not found.</p>

  return (
    <div className="test-run-detail">
      <nav className="back-links">
        <Link to={`/sprints/${sprintId}/test-runs`} className="back-link">
          &larr; Back to Test Runs
        </Link>
      </nav>

      <header className="test-run-detail-header">
        <h1>Test Run</h1>
        <span className={`run-badge run-badge-${run.status}`}>
          {STATUS_LABELS[run.status] ?? run.status}
        </span>
      </header>

      <p className="test-run-detail-meta">{new Date(run.created_at).toLocaleString()}</p>

      {actionError && <p className="test-run-detail-error">{actionError}</p>}

      {run.executions.map((execution) => (
        <section key={execution.id} className="test-execution-section">
          <header className="test-execution-header">
            <h2>{execution.requirement_name}</h2>
            <div className="test-execution-header-right">
              <span className={`run-badge run-badge-${execution.status}`}>
                {STATUS_LABELS[execution.status] ?? execution.status}
              </span>
              {execution.status === 'failed' && sprint.active && (
                <button
                  className="btn btn-secondary btn-small"
                  onClick={() => handleRestart(execution)}
                  disabled={restarting === execution.id}
                >
                  {restarting === execution.id ? 'Restarting…' : 'Restart'}
                </button>
              )}
            </div>
          </header>

          {execution.error && <p className="test-execution-error">{execution.error}</p>}

          <ul className="case-execution-list">
            {execution.cases.map((caseExecution) => (
              <TestCaseExecutionRow key={caseExecution.id} caseExecution={caseExecution} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}
