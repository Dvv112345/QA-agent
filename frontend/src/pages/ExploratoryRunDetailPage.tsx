import { useCallback } from 'react'
import PageState from '../components/PageState'
import { Link, useParams } from 'react-router-dom'
import ExportSummary from '../components/ExportSummary'
import OutdatedBadge from '../components/OutdatedBadge'
import RestartControl from '../components/RestartControl'
import {
  exportExploratoryRunFindings,
  fetchExploratoryRun,
  restartExploratoryRun,
  summarizeExploratoryRun,
} from '../services/api'
import type { ExploratoryRunDetailResponse } from '../types'
import { awaitingExport } from '../exportState'
import { plural } from '../format'
import { useAction } from '../hooks/useAction'
import { useCrumb } from '../BreadcrumbContext'
import { useAsyncData } from '../hooks/useAsyncData'
import { EXPORT_GRACE_TICKS, usePolling } from '../hooks/usePolling'
import { EXPLORATORY_RUN_STATUS_LABELS, SESSION_STATUS_LABELS } from '../statusLabels'
import './ExploratoryRunDetailPage.css'

export default function ExploratoryRunDetailPage() {
  const { id, runId } = useParams<{ id: string; runId: string }>()
  const sprintId = Number(id)
  const exploratoryRunId = Number(runId)

  const {
    data: run,
    loading,
    error: loadError,
    setData: setRun,
  } = useAsyncData(() => fetchExploratoryRun(exploratoryRunId), [exploratoryRunId])

  const onLoaded = useCallback((fresh: ExploratoryRunDetailResponse) => setRun(fresh), [setRun])
  const { busy, error: actionError, run: runAction } = useAction(onLoaded)

  const inProgress = run?.status === 'pending' || run?.status === 'running'
  const exportPending = run !== null && awaitingExport(run)

  usePolling(() => fetchExploratoryRun(exploratoryRunId).then(setRun), {
    enabled: inProgress || exportPending,
    // Unbounded while the run itself is working — it is the run that says
    // when that ends. Bounded once only the export is outstanding.
    maxTicks: inProgress ? undefined : EXPORT_GRACE_TICKS,
  })

  useCrumb('run', run ? `Exploratory Run #${run.id}` : null)

  if (loading) return <PageState kind="loading">Loading exploratory run&hellip;</PageState>
  if (loadError) return <PageState kind="error">{loadError}</PageState>
  if (!run) return <PageState kind="empty">Exploratory run not found.</PageState>

  return (
    <div className="exp-run">
      <nav className="page-back">
        <Link to={`/sprints/${sprintId}/test-runs`} className="back-link">
          &larr; Back to Test Runs
        </Link>
      </nav>

      <header className="exp-run-header">
        <h1>{run.requirement_name}</h1>
        {/* The id is shown because a filed ticket names it ("Exploratory
            run 10") and it otherwise lives only in the URL. Global rather
            than per-sprint, hence `#10` — an identifier, not a count. */}
        <span className="exp-run-id">Run #{run.id}</span>
        <span className={`run-badge run-badge-${run.status}`}>
          {EXPLORATORY_RUN_STATUS_LABELS[run.status]}
        </span>
        <OutdatedBadge run={run} />
      </header>

      <p className="exp-run-counts">
        {plural(run.sessions.length, 'session')} &middot; {plural(run.bug_count, 'bug')} &middot;{' '}
        {plural(run.issue_count, 'issue')}
        {run.high_severity_count > 0 && ` · ${run.high_severity_count} high severity`}
      </p>

      <ExportSummary
        rollup={run}
        onExport={() => exportExploratoryRunFindings(exploratoryRunId).then(setRun)}
      />

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
              onClick={() => runAction(summarizeExploratoryRun(exploratoryRunId))}
              disabled={busy}
            >
              {busy ? 'Generating…' : 'Generate summary'}
            </button>
          </>
        ) : (
          <p className="exp-run-muted">Available once the run finishes.</p>
        )}
      </section>

      <RestartControl
        run={run}
        enabled={run.status === 'failed'}
        busy={busy}
        label="Restart run"
        outdatedNote="Start a new exploratory run to retest — this one used earlier content."
        noteClassName="exp-run-muted"
        onRestart={() => runAction(restartExploratoryRun(exploratoryRunId))}
      />

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
                    {SESSION_STATUS_LABELS[session.status]}
                  </span>
                </div>
                <div className="exp-session-meta">
                  <span>{session.sfdipot_areas.join(', ') || 'No areas tagged'}</span>
                  <span>{session.actions_used} actions</span>
                  <span>{plural(session.finding_count, 'finding')}</span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
