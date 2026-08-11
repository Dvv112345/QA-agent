import { Link, useParams } from 'react-router-dom'
import FindingCard from '../components/FindingCard'
import { fetchExploratorySession, findingScreenshotUrl } from '../services/api'
import { useAsyncData } from '../hooks/useAsyncData'
import { useCrumb } from '../BreadcrumbContext'
import { usePolling } from '../hooks/usePolling'
import { SESSION_STATUS_LABELS, STOP_REASON_LABELS } from '../statusLabels'
import './ExploratorySessionPage.css'

export default function ExploratorySessionPage() {
  const { id, sessionId } = useParams<{ id: string; sessionId: string }>()
  const sprintId = Number(id)
  const exploratorySessionId = Number(sessionId)

  const {
    data: session,
    loading,
    error: loadError,
    setData: setSession,
  } = useAsyncData(() => fetchExploratorySession(exploratorySessionId), [exploratorySessionId])

  const inProgress = session?.status === 'pending' || session?.status === 'running'

  usePolling(() => fetchExploratorySession(exploratorySessionId).then(setSession), {
    enabled: !!inProgress,
  })

  // The parent run is not in this URL — it comes from the fetched session.
  useCrumb(
    'run',
    session ? `Exploratory Run #${session.exploratory_run_id}` : null,
    session ? `/sprints/${sprintId}/exploratory-runs/${session.exploratory_run_id}` : undefined,
  )

  if (loading) return <p className="exp-session-message">Loading session sheet&hellip;</p>
  if (loadError) return <p className="exp-session-message exp-session-error">{loadError}</p>
  if (!session) return <p className="exp-session-message">Session not found.</p>

  return (
    <div className="exp-session">
      <nav className="page-back">
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
          {SESSION_STATUS_LABELS[session.status]}
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
              <FindingCard
                key={finding.id}
                finding={finding}
                screenshotUrl={finding.has_screenshot ? findingScreenshotUrl(finding.id) : null}
              />
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
