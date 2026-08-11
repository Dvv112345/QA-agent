import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import PageState from '../components/PageState'
import FindingCard from '../components/FindingCard'
import FinishSprintControl from '../components/FinishSprintControl'
import { fetchExploratorySession, fetchSprint, findingScreenshotUrl } from '../services/api'
import type { SprintResponse } from '../types'
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

  /*
   * The sprint is fetched only so this page can offer Finish Sprint and name
   * the sprint in the breadcrumb. It is kept out of the session's `useAsyncData`
   * on purpose: the session polls while it runs, and folding the sprint in would
   * mean re-reading it every 2.5 s for a value that does not move. A failure
   * here costs the button and the crumb label, nothing else.
   */
  const [sprint, setSprint] = useState<SprintResponse | null>(null)
  useEffect(() => {
    let cancelled = false
    fetchSprint(sprintId)
      .then((data) => {
        if (!cancelled) setSprint(data)
      })
      .catch(() => {
        /* the session sheet is what this page is for — it renders regardless */
      })
    return () => {
      cancelled = true
    }
  }, [sprintId])

  useCrumb('sprint', sprint?.name)

  // The parent run is not in this URL — it comes from the fetched session.
  useCrumb(
    'run',
    session ? `Exploratory Run #${session.exploratory_run_id}` : null,
    session ? `/sprints/${sprintId}/exploratory-runs/${session.exploratory_run_id}` : undefined,
  )

  if (loading) return <PageState kind="loading">Loading session sheet&hellip;</PageState>
  if (loadError) return <PageState kind="error">{loadError}</PageState>
  if (!session) return <PageState kind="empty">Session not found.</PageState>

  return (
    <div className="exp-session">
      <FinishSprintControl sprint={sprint} onFinished={setSprint} />

      <nav className="page-nav">
        <Link
          to={`/sprints/${sprintId}/exploratory-runs/${session.exploratory_run_id}`}
          className="btn btn-secondary"
          aria-label="Back to Exploratory Run"
        >
          &larr; Back
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
