import { useCallback, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import CicdConfigModal from '../components/CicdConfigModal'
import FinishSprintControl from '../components/FinishSprintControl'
import PageState from '../components/PageState'
import { useCrumb } from '../BreadcrumbContext'
import { useAction } from '../hooks/useAction'
import { useAsyncData } from '../hooks/useAsyncData'
import { usePolling } from '../hooks/usePolling'
import {
  createCicdExport,
  fetchCicdConfig,
  fetchCicdEligibility,
  fetchCicdExports,
  fetchSprint,
  restartCicdExport,
} from '../services/api'
import { CICD_EXPORT_STATUS_LABELS } from '../statusLabels'
import { formatDateTime, plural } from '../format'
import type {
  CicdCaseEntry,
  CicdConfig,
  CicdEligibility,
  CicdExport,
  SprintResponse,
} from '../types'
import './CicdPage.css'

/** Which upstream artifact moved, as a reader's phrase rather than a column name. */
const STALE_REASON_LABELS: Record<string, string> = {
  requirement: 'the requirement changed',
  test_plan: 'the test plan changed',
  test_environment: 'the test environment changed',
  unknown: 'it predates change tracking',
}

/**
 * Why a case cannot be exported, in the terms of what to do about it.
 *
 * The two reasons imply *different actions* — run this case at all, versus
 * run it again — which is the whole reason they are separate states rather
 * than one "not exportable".
 */
function ineligibleReason(entry: CicdCaseEntry): string {
  if (entry.reason === 'no_script') return 'No script yet — run this test case first'
  const reasons = entry.stale_reasons.map((r) => STALE_REASON_LABELS[r] ?? r).join(', ')
  return `Out of date — ${reasons}. Re-run this test case`
}

export default function CicdPage() {
  const { id } = useParams<{ id: string }>()
  const sprintId = Number(id)

  const [sprint, setSprint] = useState<SprintResponse | null>(null)
  const [config, setConfig] = useState<CicdConfig | null>(null)
  const [eligibility, setEligibility] = useState<CicdEligibility | null>(null)
  const [exports, setExports] = useState<CicdExport[]>([])
  const [selected, setSelected] = useState<Set<number> | null>(null)
  const [showConfigModal, setShowConfigModal] = useState(false)

  const { loading, error } = useAsyncData(async () => {
    const [sprintData, configData, eligibilityData, exportData] = await Promise.all([
      fetchSprint(sprintId),
      fetchCicdConfig(sprintId),
      fetchCicdEligibility(sprintId),
      fetchCicdExports(sprintId),
    ])
    setSprint(sprintData)
    setConfig(configData)
    setEligibility(eligibilityData)
    setExports(exportData)
    // Every eligible case starts checked, minus the ones a completed export
    // already shipped: re-exporting those is legitimate but rarely what the
    // user came for, so it is opt-in rather than opt-out.
    setSelected(
      new Set(
        eligibilityData.entries
          .filter((entry) => entry.eligible && !entry.previously_exported)
          .map((entry) => entry.test_case_id),
      ),
    )
    return sprintData
  }, [sprintId])

  useCrumb('sprint', sprint?.name, sprint ? `/sprints/${sprint.id}` : undefined)

  const refresh = useCallback(async () => {
    const [exportData, eligibilityData] = await Promise.all([
      fetchCicdExports(sprintId),
      fetchCicdEligibility(sprintId),
    ])
    setExports(exportData)
    setEligibility(eligibilityData)
  }, [sprintId])

  // No EXPORT_GRACE_TICKS analogue is needed: unlike a test run, this row
  // reaches `completed` only *after* its pull request exists, so nothing
  // lands after the terminal read.
  const inFlight = exports.some((row) => row.status === 'pending' || row.status === 'running')
  usePolling(refresh, { enabled: inFlight })

  const exportAction = useAction<CicdExport>(() => {
    void refresh()
  })

  // Keyed on the eligibility object itself rather than on a `?? []` fallback,
  // which would be a fresh array every render and rebuild the map each time.
  const grouped = useMemo(() => {
    const byRequirement = new Map<number, { name: string; entries: CicdCaseEntry[] }>()
    for (const entry of eligibility?.entries ?? []) {
      const bucket = byRequirement.get(entry.requirement_id)
      if (bucket) bucket.entries.push(entry)
      else {
        byRequirement.set(entry.requirement_id, {
          name: entry.requirement_name,
          entries: [entry],
        })
      }
    }
    return [...byRequirement.values()]
  }, [eligibility])

  if (loading) return <PageState kind="loading">Loading…</PageState>
  if (error) return <PageState kind="error">{error}</PageState>
  if (!sprint || !eligibility || selected === null) {
    return <PageState kind="error">Sprint not found.</PageState>
  }

  const toggle = (caseId: number) => {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(caseId)) next.delete(caseId)
      else next.add(caseId)
      return next
    })
  }

  const handleExport = () => {
    void exportAction.run(createCicdExport(sprintId, [...selected]))
  }

  const canExport = config !== null && selected.size > 0 && !exportAction.busy

  return (
    <div className="cicd">
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

      <header className="cicd-header">
        <h1>Export to CI/CD</h1>
      </header>

      <p className="cicd-sprint-name">{sprint.name}</p>

      <div className="cicd-config-panel">
        {config ? (
          <>
            <span className="cicd-config-panel-label">
              {config.provider === 'jenkins' ? 'Jenkins' : 'GitHub Actions'}
            </span>
            <span className="cicd-config-panel-hint">
              verified {formatDateTime(config.verified_at)}
            </span>
            <button
              className="btn btn-secondary btn-small"
              onClick={() => setShowConfigModal(true)}
            >
              Change
            </button>
          </>
        ) : (
          <>
            <span className="cicd-config-panel-none">No CI/CD target connected.</span>
            <button
              className="btn btn-secondary btn-small"
              onClick={() => setShowConfigModal(true)}
            >
              Connect GitHub Actions or Jenkins
            </button>
          </>
        )}
      </div>

      {(eligibility.variable_names.length > 0 || eligibility.secret_names.length > 0) && (
        <section className="cicd-env-names">
          <h2>What the team will need to create</h2>
          <p className="cicd-env-names-hint">
            The generated job references these by name. Values are never written into the
            repository, so they have to exist on the CI side before it can run.
          </p>
          <dl>
            {eligibility.variable_names.length > 0 && (
              <>
                <dt>Variables</dt>
                <dd>
                  {eligibility.variable_names.map((name) => (
                    <code key={name}>{name}</code>
                  ))}
                </dd>
              </>
            )}
            {eligibility.secret_names.length > 0 && (
              <>
                <dt>Secrets</dt>
                <dd>
                  {eligibility.secret_names.map((name) => (
                    <code key={name}>{name}</code>
                  ))}
                </dd>
              </>
            )}
          </dl>
        </section>
      )}

      <section className="cicd-section">
        <div className="cicd-section-header">
          <h2>Test cases</h2>
          <button className="btn btn-primary" onClick={handleExport} disabled={!canExport}>
            {exportAction.busy ? 'Starting…' : `Export ${selected.size || ''}`.trim()}
          </button>
        </div>

        {!config && <p className="cicd-notice">Connect a CI/CD target before exporting.</p>}
        {exportAction.error && (
          <p className="cicd-error" role="alert">
            {exportAction.error}
          </p>
        )}

        {grouped.length === 0 ? (
          <PageState kind="empty">
            No test cases yet. Generate test plans and run them first.
          </PageState>
        ) : (
          grouped.map((group) => (
            <div key={group.name} className="cicd-requirement">
              <h3>{group.name}</h3>
              <ul className="cicd-case-list">
                {group.entries.map((entry) => (
                  <li
                    key={entry.test_case_id}
                    className={entry.eligible ? 'cicd-case' : 'cicd-case cicd-case-ineligible'}
                  >
                    <label>
                      <input
                        type="checkbox"
                        checked={selected.has(entry.test_case_id)}
                        onChange={() => toggle(entry.test_case_id)}
                        disabled={!entry.eligible}
                      />
                      <span className="cicd-case-title">{entry.case_title}</span>
                    </label>
                    {!entry.eligible && (
                      <span className="cicd-case-reason">{ineligibleReason(entry)}</span>
                    )}
                    {entry.previously_exported && entry.last_export_pr_url && (
                      <a
                        className="cicd-case-exported"
                        href={entry.last_export_pr_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        already exported
                      </a>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))
        )}
      </section>

      <section className="cicd-section">
        <h2>Export history</h2>
        {exports.length === 0 ? (
          <PageState kind="empty">Nothing exported yet.</PageState>
        ) : (
          <ul className="cicd-export-list">
            {exports.map((row) => (
              <ExportRow key={row.id} row={row} onRestarted={refresh} />
            ))}
          </ul>
        )}
      </section>

      {showConfigModal && (
        <CicdConfigModal
          sprintId={sprintId}
          config={config}
          repo={sprint.repo ?? null}
          onSaved={setConfig}
          onClose={() => setShowConfigModal(false)}
        />
      )}
    </div>
  )
}

function ExportRow({ row, onRestarted }: { row: CicdExport; onRestarted: () => Promise<void> }) {
  const restart = useAction<CicdExport>(() => {
    void onRestarted()
  })

  return (
    <li className="cicd-export">
      <div className="cicd-export-head">
        <span className={`run-badge run-badge-${row.status}`}>
          {CICD_EXPORT_STATUS_LABELS[row.status]}
        </span>
        <span className="cicd-export-when">{formatDateTime(row.created_at)}</span>
        <span className="cicd-export-count">{plural(row.case_count, 'test case')}</span>
        {row.pr_url && (
          <a href={row.pr_url} target="_blank" rel="noreferrer" className="cicd-export-pr">
            {row.pr_number ? `Pull request #${row.pr_number}` : 'Pull request'}
          </a>
        )}
      </div>

      {row.ci_file_paths.length > 0 && (
        <p className="cicd-export-files">
          CI files:{' '}
          {row.ci_file_paths.map((path) => (
            <code key={path}>{path}</code>
          ))}
        </p>
      )}

      {row.dropped_paths.length > 0 && (
        <p className="cicd-export-dropped">
          Not written:{' '}
          {row.dropped_paths.map((path) => (
            <code key={path}>{path}</code>
          ))}
        </p>
      )}

      {row.notes && <p className="cicd-export-notes">{row.notes}</p>}

      {row.status === 'failed' && (
        <div className="cicd-export-failure">
          <p className="cicd-error" role="alert">
            {row.error ?? 'This export failed.'}
          </p>
          <button
            className="btn btn-secondary btn-small"
            onClick={() => void restart.run(restartCicdExport(row.id))}
            disabled={restart.busy}
          >
            {restart.busy ? 'Restarting…' : 'Restart'}
          </button>
          {restart.error && (
            <p className="cicd-error" role="alert">
              {restart.error}
            </p>
          )}
        </div>
      )}
    </li>
  )
}
