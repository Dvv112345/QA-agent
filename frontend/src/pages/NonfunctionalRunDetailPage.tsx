import { useCallback, useEffect, useState } from 'react'
import PageState from '../components/PageState'
import { Link, useParams } from 'react-router-dom'
import ExportSummary from '../components/ExportSummary'
import FindingCard from '../components/FindingCard'
import FinishSprintControl from '../components/FinishSprintControl'
import OutdatedBadge from '../components/OutdatedBadge'
import RestartControl from '../components/RestartControl'
import {
  exportNonfunctionalRunFindings,
  fetchNonfunctionalRun,
  fetchSprint,
  nonfunctionalFindingScreenshotUrl,
  restartNonfunctionalRun,
  summarizeNonfunctionalRun,
} from '../services/api'
import type {
  DomainOutcome,
  NonfunctionalLoadProfileResponse,
  NonfunctionalRunDetailResponse,
  NonfunctionalTargetResponse,
  SprintResponse,
} from '../types'
import { awaitingExport } from '../exportState'
import { plural } from '../format'
import { useAction } from '../hooks/useAction'
import { useCrumb } from '../BreadcrumbContext'
import { useAsyncData } from '../hooks/useAsyncData'
import { EXPORT_GRACE_TICKS, usePolling } from '../hooks/usePolling'
import {
  DOMAIN_OUTCOME_LABELS,
  type DomainOutcomeDisplay,
  NONFUNCTIONAL_CHILD_STATUS_LABELS,
  NONFUNCTIONAL_RUN_STATUS_LABELS,
} from '../statusLabels'
import './NonfunctionalRunDetailPage.css'

/**
 * The three domains, in the order the panel reads them.
 *
 * `judged` is what separates a domain with an oracle from one without.
 * Accessibility is graded by axe-core and security by the fixed rule
 * table, so their `clean` is a verdict. Performance is measured and never
 * judged, so its `clean` is not — and a domain-blind cell reported one
 * anyway, in the word and in the colour.
 */
const DOMAIN_ROWS: {
  label: string
  key: keyof NonfunctionalTargetResponse
  judged: boolean
}[] = [
  { label: 'Accessibility', key: 'a11y_outcome', judged: true },
  { label: 'Security', key: 'security_outcome', judged: true },
  { label: 'Performance', key: 'performance_outcome', judged: false },
]

function OutcomeCell({ outcome, judged }: { outcome: DomainOutcome | null; judged: boolean }) {
  // Null is "not selected for this run" — a fourth thing, and saying
  // nothing about it is the honest rendering.
  if (outcome === null)
    return <span className="nf-outcome nf-outcome-unselected">Not selected</span>
  // `clean` is the only value that needs remapping: an unjudged domain can
  // still fail to run, and that reading is already correct.
  const display: DomainOutcomeDisplay = !judged && outcome === 'clean' ? 'measured' : outcome
  // One key drives the label and the colour together, so the chip cannot
  // say "Measured" in the green that means "passed".
  return (
    <span className={`nf-outcome nf-outcome-${display}`}>{DOMAIN_OUTCOME_LABELS[display]}</span>
  )
}

function Measurements({ values }: { values: Record<string, number | string | null> }) {
  const entries = Object.entries(values).filter(([, value]) => value !== null && value !== '')
  if (entries.length === 0) return null
  return (
    <dl className="nf-measurements">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{key.replace(/_/g, ' ')}</dt>
          <dd>{String(value)}</dd>
        </div>
      ))}
    </dl>
  )
}

function LoadProfilePanel({ profile }: { profile: NonfunctionalLoadProfileResponse }) {
  return (
    <li className="nf-profile-panel">
      <div className="nf-profile-head">
        <span className="nf-profile-method">{profile.method}</span>
        <span className="nf-profile-url">{profile.url}</span>
        <span className={`session-badge session-badge-${profile.status}`}>
          {NONFUNCTIONAL_CHILD_STATUS_LABELS[profile.status]}
        </span>
      </div>
      <p className="nf-profile-meta">
        {plural(profile.requests_sent, 'request')} sent of {profile.total_request_cap} approved
        &middot; concurrency {profile.concurrency} &middot; {profile.duration_seconds}s
        {/* Cookies are always on, and that is invisible in the numbers —
            so it is said here. */}
        <span className="nf-authenticated"> · ran authenticated</span>
      </p>
      {profile.error && <p className="nf-run-error">{profile.error}</p>}
      <Measurements values={profile.results} />
    </li>
  )
}

export default function NonfunctionalRunDetailPage() {
  const { id, runId } = useParams<{ id: string; runId: string }>()
  const sprintId = Number(id)
  const nonfunctionalRunId = Number(runId)

  const {
    data: run,
    loading,
    error: loadError,
    setData: setRun,
  } = useAsyncData(() => fetchNonfunctionalRun(nonfunctionalRunId), [nonfunctionalRunId])

  const onLoaded = useCallback((fresh: NonfunctionalRunDetailResponse) => setRun(fresh), [setRun])
  const { busy, error: actionError, run: runAction } = useAction(onLoaded)

  const inProgress = run?.status === 'pending' || run?.status === 'running'
  const exportPending = run !== null && awaitingExport(run)

  usePolling(() => fetchNonfunctionalRun(nonfunctionalRunId).then(setRun), {
    enabled: inProgress || exportPending,
    // Unbounded while the run itself is working; bounded once only the
    // export is outstanding — see the exploratory twin.
    maxTicks: inProgress ? undefined : EXPORT_GRACE_TICKS,
  })

  /* Fetched only for Finish Sprint and the crumb label — see the
     exploratory run page for why it is kept out of the polling read. */
  const [sprint, setSprint] = useState<SprintResponse | null>(null)
  useEffect(() => {
    let cancelled = false
    fetchSprint(sprintId)
      .then((data) => {
        if (!cancelled) setSprint(data)
      })
      .catch(() => {
        /* the run is what this page is for — it renders regardless */
      })
    return () => {
      cancelled = true
    }
  }, [sprintId])

  useCrumb('sprint', sprint?.name)
  useCrumb('run', run ? `Nonfunctional Run #${run.id}` : null)

  if (loading) return <PageState kind="loading">Loading nonfunctional run&hellip;</PageState>
  if (loadError) return <PageState kind="error">{loadError}</PageState>
  if (!run) return <PageState kind="empty">Nonfunctional run not found.</PageState>

  return (
    <div className="nf-run">
      <FinishSprintControl sprint={sprint} onFinished={setSprint} />

      <nav className="page-nav">
        <Link
          to={`/sprints/${sprintId}/test-runs`}
          className="btn btn-secondary"
          aria-label="Back to Test Runs"
        >
          &larr; Back
        </Link>
      </nav>

      <header className="nf-run-header">
        <h1>{run.requirement_name}</h1>
        <span className="nf-run-id">Run #{run.id}</span>
        <span className={`run-badge run-badge-${run.status}`}>
          {NONFUNCTIONAL_RUN_STATUS_LABELS[run.status]}
        </span>
        <OutdatedBadge run={run} />
      </header>

      <p className="nf-run-counts">
        {plural(run.targets.length, 'URL')} examined &middot; {plural(run.bug_count, 'finding')}
        {run.high_severity_count > 0 && ` · ${run.high_severity_count} high severity`}
        {run.issue_count > 0 && ` · ${plural(run.issue_count, 'issue')}`}
      </p>

      <p className="nf-run-domains">
        Checks run: {run.domains.join(', ') || 'none'}
        {run.environment_disposable && ' · environment declared disposable'}
      </p>

      <ExportSummary
        rollup={run}
        onExport={() => exportNonfunctionalRunFindings(nonfunctionalRunId).then(setRun)}
      />

      {run.error && <p className="nf-run-error">{run.error}</p>}

      <section className="nf-run-summary">
        <h2>Summary</h2>
        {run.summary ? (
          <p>{run.summary}</p>
        ) : run.status === 'completed' ? (
          <>
            <p className="nf-run-muted">
              No summary was generated for this run. The findings and measurements below are
              unaffected.
            </p>
            <button
              className="btn btn-secondary"
              onClick={() => runAction(summarizeNonfunctionalRun(nonfunctionalRunId))}
              disabled={busy}
            >
              {busy ? 'Generating…' : 'Generate summary'}
            </button>
          </>
        ) : (
          <p className="nf-run-muted">Available once the run finishes.</p>
        )}
      </section>

      <RestartControl
        run={run}
        enabled={run.status === 'failed'}
        busy={busy}
        label="Restart run"
        outdatedNote="Start a new nonfunctional run to re-examine — this one used earlier content."
        noteClassName="nf-run-muted"
        onRestart={() => runAction(restartNonfunctionalRun(nonfunctionalRunId))}
      />

      {actionError && <p className="nf-run-error">{actionError}</p>}

      <section>
        <h2>Findings</h2>
        {run.findings.length === 0 ? (
          <p className="nf-run-muted">
            {run.status === 'completed'
              ? 'No violations were found.'
              : 'Findings appear once the run finishes.'}
          </p>
        ) : (
          <ul className="nf-finding-list">
            {run.findings.map((finding) => (
              <li key={finding.id}>
                <p className="nf-finding-source">
                  <span className="nf-finding-rule">{finding.rule}</span>
                  <span className="nf-finding-domain">{finding.domain}</span>
                  <span className="nf-finding-url">{finding.url}</span>
                </p>
                <FindingCard
                  finding={finding}
                  screenshotUrl={
                    finding.has_screenshot
                      ? nonfunctionalFindingScreenshotUrl(finding.id)
                      : undefined
                  }
                />
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h2>Examined URLs</h2>
        {run.targets.length === 0 ? (
          <p className="nf-run-muted">
            {run.status === 'completed'
              ? 'This run reached no URL to examine.'
              : 'URLs appear as the run reaches them.'}
          </p>
        ) : (
          <ul className="nf-target-list">
            {run.targets.map((target) => (
              <li key={target.id} className="nf-target">
                <div className="nf-target-head">
                  <span className="nf-target-url">{target.url}</span>
                  <span className="nf-target-kind">{target.kind}</span>
                  <span className={`session-badge session-badge-${target.status}`}>
                    {NONFUNCTIONAL_CHILD_STATUS_LABELS[target.status]}
                  </span>
                </div>
                <div className="nf-target-outcomes">
                  {DOMAIN_ROWS.map(({ label, key, judged }) => (
                    <span key={label} className="nf-outcome-row">
                      <span className="nf-outcome-label">{label}</span>
                      <OutcomeCell outcome={target[key] as DomainOutcome | null} judged={judged} />
                    </span>
                  ))}
                </div>
                {target.error && <p className="nf-run-error">{target.error}</p>}
                <Measurements values={target.metrics} />
              </li>
            ))}
          </ul>
        )}
      </section>

      {run.load_profiles.length > 0 && (
        <section>
          <h2>Load profiles</h2>
          <ul className="nf-profile-panels">
            {run.load_profiles.map((profile) => (
              <LoadProfilePanel key={profile.id} profile={profile} />
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
