import { useEffect, useState } from 'react'
import PageState from '../components/PageState'
import { Link, useParams } from 'react-router-dom'
import ExploratoryCharterModal from '../components/ExploratoryCharterModal'
import FinishSprintControl from '../components/FinishSprintControl'
import IssueTrackerModal from '../components/IssueTrackerModal'
import NonfunctionalRunModal from '../components/NonfunctionalRunModal'
import OutdatedBadge from '../components/OutdatedBadge'
import RunTestModal from '../components/RunTestModal'
import SprintMetricsPanel from '../components/SprintMetricsPanel'
import {
  fetchExploratoryRuns,
  fetchIssueTracker,
  fetchNonfunctionalRuns,
  fetchSprint,
  fetchSprintMetrics,
  fetchTestPlans,
  fetchTestRuns,
} from '../services/api'
import type {
  ExploratoryRunResponse,
  IssueTrackerConfig,
  NonfunctionalRunResponse,
  SprintMetrics,
  SprintResponse,
  TestPlanResponse,
  TestRunResponse,
} from '../types'
import { awaitingExport } from '../exportState'
import { useCrumb } from '../BreadcrumbContext'
import { formatDateTime, plural } from '../format'
import { EXPORT_GRACE_TICKS, usePolling } from '../hooks/usePolling'
import {
  EXPLORATORY_RUN_STATUS_LABELS,
  NONFUNCTIONAL_RUN_STATUS_LABELS,
  RUN_STATUS_LABELS,
} from '../statusLabels'
import './TestRunsPage.css'

function resultSummary(run: TestRunResponse): string {
  const parts: string[] = []
  if (run.passed_cases > 0) parts.push(`${run.passed_cases} passed`)
  if (run.failed_cases > 0) parts.push(`${run.failed_cases} failed`)
  if (run.error_cases > 0) parts.push(`${run.error_cases} error`)
  return parts.join(' / ')
}

function findingSummary(run: ExploratoryRunResponse | NonfunctionalRunResponse): string {
  const parts: string[] = []
  if (run.bug_count > 0) parts.push(plural(run.bug_count, 'bug'))
  if (run.issue_count > 0) parts.push(plural(run.issue_count, 'issue'))
  if (parts.length === 0 && run.status === 'completed') return 'No findings'
  return parts.join(' / ')
}

export default function TestRunsPage() {
  const { id } = useParams<{ id: string }>()
  const sprintId = Number(id)

  const [sprint, setSprint] = useState<SprintResponse | null>(null)
  const [runs, setRuns] = useState<TestRunResponse[]>([])
  const [exploratoryRuns, setExploratoryRuns] = useState<ExploratoryRunResponse[]>([])
  const [nonfunctionalRuns, setNonfunctionalRuns] = useState<NonfunctionalRunResponse[]>([])
  const [metrics, setMetrics] = useState<SprintMetrics | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [showRunModal, setShowRunModal] = useState(false)
  const [showCharterModal, setShowCharterModal] = useState(false)
  const [showNonfunctionalModal, setShowNonfunctionalModal] = useState(false)
  const [tracker, setTracker] = useState<IssueTrackerConfig | null>(null)
  const [approvedPlans, setApprovedPlans] = useState<TestPlanResponse[]>([])
  const [showTrackerModal, setShowTrackerModal] = useState(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      fetchSprint(sprintId),
      fetchTestRuns(sprintId),
      fetchExploratoryRuns(sprintId),
      fetchNonfunctionalRuns(sprintId),
      // Fetched once here and passed down as props, so neither run modal
      // needs a second round trip — for its export toggle, or for the
      // requirement list it offers to run.
      fetchIssueTracker(sprintId),
      fetchTestPlans(sprintId),
      // Swallowed, unlike the four above: the panel is decoration and the
      // run lists are the page. `services/qa_metrics.py` already never
      // raises so a metrics failure cannot 500 this endpoint — but that
      // contract stops at the service boundary, and an unreachable one
      // would otherwise replace both lists with an error string. Same
      // reason the poll below swallows its own failures; `metrics` is
      // already nullable and the panel renders behind it.
      fetchSprintMetrics(sprintId).catch(() => null),
    ])
      .then(
        ([
          sprintData,
          runData,
          exploratoryData,
          nonfunctionalData,
          trackerData,
          planData,
          metricsData,
        ]) => {
          if (!cancelled) {
            setSprint(sprintData)
            setRuns(runData)
            setExploratoryRuns(exploratoryData)
            setNonfunctionalRuns(nonfunctionalData)
            setTracker(trackerData)
            setApprovedPlans(planData.filter((plan) => plan.status === 'approved'))
            setMetrics(metricsData)
            setLoading(false)
          }
        },
      )
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

  const inProgress =
    runs.some((run) => run.status === 'running') ||
    exploratoryRuns.some((run) => run.status === 'pending' || run.status === 'running') ||
    nonfunctionalRuns.some((run) => run.status === 'pending' || run.status === 'running')
  // Keyed on `completed` rather than on any terminal status, and on there
  // being no export error: every other way to reach "unfiled bugs" is a
  // standing state, not a pending one. A failed run never calls export at
  // all, and every known bad ending inside it writes a tracker error.
  const exportPending =
    runs.some(awaitingExport) ||
    exploratoryRuns.some(awaitingExport) ||
    nonfunctionalRuns.some(awaitingExport)
  const shouldPoll = inProgress || exportPending

  usePolling(
    () =>
      // The metrics ride along with the run lists rather than on their own
      // interval: the panel summarizes exactly these rows, so refreshing
      // them apart would let the two disagree on screen.
      Promise.all([
        fetchTestRuns(sprintId),
        fetchExploratoryRuns(sprintId),
        fetchNonfunctionalRuns(sprintId),
        fetchSprintMetrics(sprintId),
      ]).then(([runData, exploratoryData, nonfunctionalData, metricsData]) => {
        setRuns(runData)
        setExploratoryRuns(exploratoryData)
        setNonfunctionalRuns(nonfunctionalData)
        setMetrics(metricsData)
      }),
    {
      enabled: shouldPoll,
      // Unbounded while a run is still working — it is the run that says
      // when that ends. Bounded once only the export is outstanding.
      // The panel's bug count depends on tracker keys, which arrive after
      // the run reads terminal, so this page needs the window too.
      maxTicks: inProgress ? undefined : EXPORT_GRACE_TICKS,
    },
  )

  useCrumb('sprint', sprint?.name)

  if (loading) return <PageState kind="loading">Loading test runs&hellip;</PageState>
  if (loadError) return <PageState kind="error">{loadError}</PageState>
  if (!sprint) return <PageState kind="empty">Sprint not found.</PageState>

  const active = sprint.active
  // Runs can outlive test_plans_complete becoming false again — guard on
  // absence of runs only, like the other stages' guard shape. Exploration
  // shares the scripted gate, so both lists sit behind it.
  // Every list must be counted here, not just the first two: a run of any
  // mode outliving `test_plans_complete` going false again would otherwise
  // be hidden behind the gate notice the moment the gate closes.
  const guarded =
    runs.length === 0 &&
    exploratoryRuns.length === 0 &&
    nonfunctionalRuns.length === 0 &&
    (!active || !sprint.test_plans_complete)

  return (
    <div className="test-runs">
      <FinishSprintControl sprint={sprint} onFinished={setSprint} />

      <nav className="page-nav">
        <Link
          to={`/sprints/${sprintId}/test-plans`}
          className="btn btn-secondary"
          aria-label="Back to Test Plans"
        >
          &larr; Back
        </Link>
      </nav>

      <header className="test-runs-header">
        <h1>Test Runs</h1>
      </header>

      <p className="test-runs-sprint-name">{sprint.name}</p>

      <div className="test-runs-panel">
        {tracker ? (
          <>
            <span className="test-runs-panel-label">{tracker.target_label}</span>
            <span className="test-runs-panel-hint">receives bug findings from a run</span>
            <button
              className="btn btn-secondary btn-small"
              onClick={() => setShowTrackerModal(true)}
            >
              Change
            </button>
          </>
        ) : (
          <>
            <span className="test-runs-panel-none">No issue tracker connected.</span>
            <button
              className="btn btn-secondary btn-small"
              onClick={() => setShowTrackerModal(true)}
            >
              Connect Jira or GitHub Issues
            </button>
          </>
        )}
      </div>

      <div className="test-runs-panel">
        <span className="test-runs-panel-hint">
          Commit the verified test scripts to this repository&rsquo;s own CI.
        </span>
        <Link to={`/sprints/${sprintId}/cicd`} className="btn btn-secondary btn-small">
          Export scripts to CI/CD
        </Link>
      </div>

      {guarded ? (
        <p className="test-runs-notice">
          {active ? 'Approve every test plan first.' : 'This sprint is finished.'}
        </p>
      ) : (
        <>
          {metrics && <SprintMetricsPanel metrics={metrics} />}

          <section className="test-runs-section">
            <div className="test-runs-section-header">
              <h2>Exploratory Sessions</h2>
              {active && sprint.test_plans_complete && (
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
                          {EXPLORATORY_RUN_STATUS_LABELS[run.status]}
                        </span>
                        <OutdatedBadge run={run} />
                      </div>
                      <div className="test-run-row-meta">
                        {/* What a filed ticket calls this run — two runs of
                            one requirement are otherwise told apart only by
                            their timestamps. */}
                        <span className="test-run-id">Run #{run.id}</span>
                        <time>{formatDateTime(run.created_at)}</time>
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
              <h2>Nonfunctional Checks</h2>
              {active && sprint.test_plans_complete && (
                <button className="btn btn-primary" onClick={() => setShowNonfunctionalModal(true)}>
                  Start nonfunctional testing
                </button>
              )}
            </div>

            {nonfunctionalRuns.length === 0 ? (
              <p className="test-runs-empty">No nonfunctional runs yet.</p>
            ) : (
              <ul className="test-runs-list">
                {nonfunctionalRuns.map((run) => (
                  <li key={run.id} className="test-run-row">
                    <Link
                      to={`/sprints/${sprintId}/nonfunctional-runs/${run.id}`}
                      className="test-run-link"
                    >
                      <div className="test-run-row-main">
                        <span className="test-run-requirements">{run.requirement_name}</span>
                        <span className={`run-badge run-badge-${run.status}`}>
                          {NONFUNCTIONAL_RUN_STATUS_LABELS[run.status]}
                        </span>
                        <OutdatedBadge run={run} />
                      </div>
                      <div className="test-run-row-meta">
                        <span className="test-run-id">Run #{run.id}</span>
                        <time>{formatDateTime(run.created_at)}</time>
                        <span>{run.domains.join(', ')}</span>
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
              {active && sprint.test_plans_complete && (
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
                          {RUN_STATUS_LABELS[run.status]}
                        </span>
                        <OutdatedBadge run={run} />
                      </div>
                      <div className="test-run-row-meta">
                        <span className="test-run-id">Run #{run.id}</span>
                        <time>{formatDateTime(run.created_at)}</time>
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

      {showRunModal && (
        <RunTestModal
          sprintId={sprintId}
          plans={approvedPlans}
          tracker={tracker}
          onClose={() => setShowRunModal(false)}
        />
      )}
      {showCharterModal && (
        <ExploratoryCharterModal
          sprintId={sprintId}
          plans={approvedPlans}
          tracker={tracker}
          onClose={() => setShowCharterModal(false)}
        />
      )}
      {showNonfunctionalModal && (
        <NonfunctionalRunModal
          sprintId={sprintId}
          plans={approvedPlans}
          tracker={tracker}
          onClose={() => setShowNonfunctionalModal(false)}
        />
      )}
      {showTrackerModal && (
        <IssueTrackerModal
          sprintId={sprintId}
          config={tracker}
          repo={sprint.repo}
          onSaved={setTracker}
          onClose={() => setShowTrackerModal(false)}
        />
      )}
    </div>
  )
}
