import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import FindingCard from '../components/FindingCard'
import { fetchExploratorySession } from '../services/api'
import type { ExploratorySessionResponse } from '../types'
import './ExploratorySessionPage.css'

const STATUS_LABELS: Record<string, string> = {
  pending: 'Queued',
  running: 'Exploring',
  completed: 'Completed',
  error: 'Error',
}

const STOP_REASON_LABELS: Record<string, string> = {
  charter_complete: 'Charter explored',
  action_cap: 'Time box exhausted',
  error: 'Stopped by an error',
}

export default function ExploratorySessionPage() {
  const { id, sessionId } = useParams<{ id: string; sessionId: string }>()
  const sprintId = Number(id)
  const exploratorySessionId = Number(sessionId)

  const [session, setSession] = useState<ExploratorySessionResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchExploratorySession(exploratorySessionId)
      .then((data) => {
        if (cancelled) return
        setSession(data)
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
  }, [exploratorySessionId])

  if (loading) return <p className="exp-session-message">Loading session sheet&hellip;</p>
  if (loadError) return <p className="exp-session-message exp-session-error">{loadError}</p>
  if (!session) return <p className="exp-session-message">Session not found.</p>

  return (
    <div className="exp-session">
      <nav className="back-links">
        <Link
          to={`/sprints/${sprintId}/exploratory-runs/${session.exploratory_run_id}`}
          className="back-link"
        >
          &larr; Back to Exploratory Run
        </Link>
      </nav>

      <header className="exp-session-header">
        <h1>Session Sheet</h1>
        <span className={`session-badge session-badge-${session.status}`}>
          {STATUS_LABELS[session.status] ?? session.status}
        </span>
      </header>

      <dl className="exp-session-meta-list">
        <dt>Charter</dt>
        <dd className="exp-session-charter">{session.charter}</dd>
        <dt>Areas covered</dt>
        <dd>{session.sfdipot_areas.join(', ') || 'None tagged'}</dd>
        <dt>Actions used</dt>
        <dd>{session.actions_used}</dd>
        <dt>Stopped because</dt>
        <dd>
          {session.stop_reason
            ? (STOP_REASON_LABELS[session.stop_reason] ?? session.stop_reason)
            : '—'}
        </dd>
      </dl>

      {session.error && <p className="exp-session-error">{session.error}</p>}

      <section>
        <h2>Test notes</h2>
        {session.session_notes ? (
          <p className="exp-session-notes">{session.session_notes}</p>
        ) : (
          <p className="exp-session-muted">No notes were recorded.</p>
        )}
      </section>

      <section>
        <h2>Findings{session.findings.length > 0 && ` (${session.findings.length})`}</h2>
        {session.findings.length === 0 ? (
          <p className="exp-session-muted">This session recorded no findings.</p>
        ) : (
          <div className="exp-session-findings">
            {session.findings.map((finding) => (
              <FindingCard key={finding.id} finding={finding} />
            ))}
          </div>
        )}
      </section>

      {session.action_log && (
        <details className="exp-session-log">
          <summary>Action log</summary>
          <pre>{session.action_log}</pre>
        </details>
      )}
    </div>
  )
}
