import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import ExportSummary from '../components/ExportSummary'
import OutdatedBadge from '../components/OutdatedBadge'
import RestartControl from '../components/RestartControl'
import TestCaseExecutionRow from '../components/TestCaseExecutionRow'
import {
  exportTestRunFindings,
  fetchSprint,
  fetchTestRun,
  restartTestExecution,
} from '../services/api'
import type { SprintResponse, TestExecutionResponse, TestRunDetailResponse } from '../types'
import { awaitingExport } from '../exportState'
import { formatDateTime } from '../format'
import { EXPORT_GRACE_TICKS, usePolling } from '../hooks/usePolling'
import { useAsyncData } from '../hooks/useAsyncData'
import { RUN_STATUS_LABELS } from '../statusLabels'
import './TestRunDetailPage.css'

export default function TestRunDetailPage() {
  const { id, runId } = useParams<{ id: string; runId: string }>()
  const sprintId = Number(id)
  const runIdNum = Number(runId)

  const [restarting, setRestarting] = useState<number | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  // Sprint and run load together and are held as one value, so polling and
  // the restart action both write through the same setter — there is only
  // ever one copy of the run on this page.
  const {
    data,
    loading,
    error: loadError,
    setData,
  } = useAsyncData(
    () =>
      Promise.all([fetchSprint(sprintId), fetchTestRun(runIdNum)]).then(([sprint, run]) => ({
        sprint,
        run,
      })),
    [sprintId, runIdNum],
  )
  const sprint: SprintResponse | null = data?.sprint ?? null
  const run: TestRunDetailResponse | null = data?.run ?? null

  const setRun = (updater: (prev: TestRunDetailResponse) => TestRunDetailResponse) =>
    setData((prev) => (prev ? { ...prev, run: updater(prev.run) } : prev))

  const inProgress = run?.status === 'running'
  const exportPending = run !== null && awaitingExport(run)

  usePolling(() => fetchTestRun(runIdNum).then((fresh) => setRun(() => fresh)), {
    enabled: inProgress || exportPending,
    // Unbounded while the run itself is working — it is the run that says
    // when that ends. Bounded once only the export is outstanding.
    maxTicks: inProgress ? undefined : EXPORT_GRACE_TICKS,
  })

  const handleRestart = (execution: TestExecutionResponse) => {
    setRestarting(execution.id)
    setActionError(null)
    restartTestExecution(execution.id)
      .then((updated) => {
        setRun((prev) => ({
          ...prev,
          executions: prev.executions.map((e) => (e.id === updated.id ? { ...e, ...updated } : e)),
        }))
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
        <Link to="/" className="back-link">
          &larr; Back to Sprints
        </Link>
        <Link to={`/sprints/${sprintId}/test-runs`} className="back-link">
          &larr; Back to Test Runs
        </Link>
      </nav>

      <header className="test-run-detail-header">
        {/* The id is shown because a filed ticket names it ("Scripted run
            14") and it otherwise lives only in the URL. Global rather than
            per-sprint, hence `#14` — an identifier, not a count. */}
        <h1>Test Run #{run.id}</h1>
        <span className={`run-badge run-badge-${run.status}`}>{RUN_STATUS_LABELS[run.status]}</span>
        <OutdatedBadge run={run} />
      </header>

      <p className="test-run-detail-meta">{formatDateTime(run.created_at)}</p>

      <ExportSummary
        rollup={run}
        onExport={() => exportTestRunFindings(runIdNum).then((fresh) => setRun(() => fresh))}
      />

      {actionError && <p className="test-run-detail-error">{actionError}</p>}

      {run.executions.map((execution) => (
        <section key={execution.id} className="test-execution-section">
          <header className="test-execution-header">
            <h2>{execution.requirement_name}</h2>
            <div className="test-execution-header-right">
              <span className={`run-badge run-badge-${execution.status}`}>
                {RUN_STATUS_LABELS[execution.status]}
              </span>
              <OutdatedBadge run={execution} />
              <RestartControl
                run={execution}
                enabled={execution.status === 'failed' && sprint.active}
                busy={restarting === execution.id}
                label="Restart"
                outdatedNote="Start a new run to retest — this one used earlier content."
                noteClassName="test-execution-outdated-note"
                buttonClassName="btn btn-secondary btn-small"
                onRestart={() => handleRestart(execution)}
              />
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
