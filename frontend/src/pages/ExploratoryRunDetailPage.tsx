import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import OutdatedBadge from '../components/OutdatedBadge'
import { isOutdated } from '../outdated'
import {
  fetchExploratoryRun,
  restartExploratoryRun,
  summarizeExploratoryRun,
} from '../services/api'
import type { ExploratoryRunDetailResponse } from '../types'
import './ExploratoryRunDetailPage.css'

const POLL_INTERVAL_MS = 2500

const RUN_STATUS_LABELS: Record<string, string> = {
  pending: 'Queued',
  running: 'Exploring',
  completed: 'Completed',
  failed: 'Failed',
}

const SESSION_STATUS_LABELS: Record<string, string> = {
  pending: 'Queued',
  running: 'Exploring',
  completed: 'Completed',
  error: 'Error',
  skipped: 'Not explored',
}

export default function ExploratoryRunDetailPage() {
  const { id, runId } = useParams<{ id: string; runId: string }>()
  const sprintId = Number(id)
  const exploratoryRunId = Number(runId)

  const [run, setRun] = useState<ExploratoryRunDetailResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const fetchingRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    fetchExploratoryRun(exploratoryRunId)
      .then((data) => {
        if (cancelled) return
        setRun(data)
        setLoading(false)
      })
      .catch((err: Error) => {
        if (cancelled) return
        setLoadError(err.message)
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [exploratoryRunId])

  const inProgress = run?.status === 'pending' || run?.status === 'running'

  useEffect(() => {
    if (!inProgress) return
    const pollId = setInterval(() => {
      if (fetchingRef.current) return
      fetchingRef.current = true
      fetchExploratoryRun(exploratoryRunId)
        .then(setRun)
        .catch(() => {
          /* transient poll failure — retry on next tick */
        })
        .finally(() => {
          fetchingRef.current = false
        })
    }, POLL_INTERVAL_MS)
    return () => clearInterval(pollId)
  }, [inProgress, exploratoryRunId])

  const runAction = useCallback(
    (action: (runId: number) => Promise<ExploratoryRunDetailResponse>) => {
      setBusy(true)
      setActionError(null)
      action(exploratoryRunId)
        .then(setRun)
        .catch((err: Error) => setActionError(err.message))
        .finally(() => setBusy(false))
    },
    [exploratoryRunId],
  )

  if (loading) return <p className="exp-run-message">Loading exploratory run&hellip;</p>
  if (loadError) return <p className="exp-run-message exp-run-error">{loadError}</p>
  if (!run) return <p className="exp-run-message">Exploratory run not found.</p>

  return (
    <div className="exp-run">
      <nav className="back-links">
        <Link to={`/sprints/${sprintId}/test-runs`} className="back-link">
          &larr; Back to Test Runs
        </Link>
      </nav>

      <header className="exp-run-header">
        <h1>{run.requirement_name}</h1>
        <span className={`run-badge run-badge-${run.status}`}>
          {RUN_STATUS_LABELS[run.status] ?? run.status}
        </span>
        <OutdatedBadge run={run} />
      </header>

      <p className="exp-run-counts">
        {run.sessions.length} session
        {run.sessions.length === 1 ? '' : 's'} &middot; {run.bug_count} bug
        {run.bug_count === 1 ? '' : 's'} &middot; {run.issue_count} issue
        {run.issue_count === 1 ? '' : 's'}
        {run.high_severity_count > 0 && ` · ${run.high_severity_count} high severity`}
      </p>

      {run.error && <p className="exp-run-error">{run.error}</p>}

      <section className="exp-run-summary">
        <h2>Summary</h2>
        {run.summary ? (
          <p>{run.summary}</p>
        ) : run.status === 'completed' ? (
          <>
            {/* The summary is best-effort: it can be absent after one
                transient provider failure, and the session sheets below are
                the real deliverable either way. */}
            <p className="exp-run-muted">
              No summary was generated for this run. The session notes and findings below are
              unaffected.
            </p>
            <button
              className="btn btn-secondary"
              onClick={() => runAction(summarizeExploratoryRun)}
              disabled={busy}
            >
              {busy ? 'Generating…' : 'Generate summary'}
            </button>
          </>
        ) : (
          <p className="exp-run-muted">Available once the run finishes.</p>
        )}
      </section>

      {/* An outdated run cannot be restarted — it would re-explore against
          content it was never chartered for. The backend refuses it too. */}
      {run.status === 'failed' && !isOutdated(run) && (
        <button
          className="btn btn-primary"
          onClick={() => runAction(restartExploratoryRun)}
          disabled={busy}
        >
          {busy ? 'Restarting…' : 'Restart run'}
        </button>
      )}
      {run.status === 'failed' && isOutdated(run) && (
        <p className="exp-run-muted">
          Start a new exploratory run to retest — this one used earlier content.
        </p>
      )}

      {actionError && <p className="exp-run-error">{actionError}</p>}

      <section>
        <h2>Sessions</h2>
        <ul className="exp-session-list">
          {run.sessions.map((session) => (
            <li key={session.id} className="exp-session-row">
              <Link
                to={`/sprints/${sprintId}/exploratory-sessions/${session.id}`}
                className="exp-session-link"
              >
                <div className="exp-session-main">
                  <span className="exp-session-charter">{session.charter}</span>
                  <span className={`session-badge session-badge-${session.status}`}>
                    {SESSION_STATUS_LABELS[session.status] ?? session.status}
                  </span>
                </div>
                <div className="exp-session-meta">
                  <span>{session.sfdipot_areas.join(', ') || 'No areas tagged'}</span>
                  <span>{session.actions_used} actions</span>
                  <span>
                    {session.finding_count} finding{session.finding_count === 1 ? '' : 's'}
                  </span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
