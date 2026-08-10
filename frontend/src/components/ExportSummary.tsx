import { useState } from 'react'
import type { ExportRollup } from '../types'
import { plural } from '../format'
import './ExportSummary.css'

interface Props {
  /**
   * The run's export state. Source-agnostic, exactly like `FindingCard`:
   * both run types compute the same roll-up server-side, so the wording
   * cannot drift between a scripted run and an exploratory one.
   */
  rollup: ExportRollup
  /** Files the run's unfiled findings and returns the refreshed run. */
  onExport: () => Promise<void>
}

export default function ExportSummary({ rollup, onExport }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const {
    export_findings: autoFiling,
    exported_finding_count: exported,
    exported_issue_count: issues,
    export_error_count: errors,
    unexported_finding_count: unexported,
    export_groups: groups,
  } = rollup

  // Nothing filed and nothing to file — this run has no bugs, so an
  // export section would be a heading over an empty space.
  if (exported === 0 && unexported === 0) return null

  const handleExport = () => {
    setBusy(true)
    setError(null)
    onExport()
      .catch((err: Error) => setError(err.message))
      .finally(() => setBusy(false))
  }

  return (
    <section className="export-summary">
      {exported > 0 && (
        <p className="export-summary-line">
          {/* Both totals, so grouping reads as grouping rather than as
              findings having gone missing. */}
          {plural(exported, 'bug')} filed as {plural(issues, 'issue')}
        </p>
      )}

      {groups.length > 0 && (
        <ul className="export-summary-groups">
          {groups.map((group) => (
            <li key={group.issue_key}>
              {/* An unrecorded URL costs the link, never the key — see
                  FindingCard, which gates on the same field. */}
              {group.issue_url ? (
                <a href={group.issue_url} target="_blank" rel="noreferrer">
                  {group.issue_key} ↗
                </a>
              ) : (
                <span>{group.issue_key}</span>
              )}{' '}
              — {plural(group.finding_count, 'finding')}
            </li>
          ))}
        </ul>
      )}

      {unexported > 0 && (
        <div className="export-summary-pending">
          <p className="export-summary-line">
            {errors > 0
              ? `${plural(errors, 'finding')} could not be filed.`
              : `${plural(unexported, 'bug')} not yet filed.`}
          </p>
          {/* Said only in the state it distinguishes: a run that was never
              set to file did not fail at anything, and reading "not yet
              filed" alone would suggest it had. The button still works —
              pressing it is itself the instruction the toggle stands in
              for. */}
          {!autoFiling && errors === 0 && (
            <p className="export-summary-note">
              This run was not set to file findings automatically.
            </p>
          )}
          {/* The `unexported` half is the whole manual path, not a
              fallback: a run that ended any way other than completed
              arrives here with its bugs unfiled by design. The label says
              which case this is. */}
          <button className="btn btn-secondary btn-small" onClick={handleExport} disabled={busy}>
            {busy ? 'Filing…' : errors > 0 ? 'Retry' : `File ${plural(unexported, 'bug')}`}
          </button>
        </div>
      )}

      {error && <p className="export-summary-error">{error}</p>}
    </section>
  )
}
