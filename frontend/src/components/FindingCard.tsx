import type { Finding } from '../types'
import './FindingCard.css'

interface Props {
  finding: Finding
  /**
   * Screenshot to show, when there is one. Passed in rather than derived
   * here so the card stays source-agnostic: exploratory findings build it
   * from their own id, and a scripted finding never has one — a subprocess
   * has no page to photograph.
   */
  screenshotUrl?: string | null
}

const TYPE_LABELS: Record<string, string> = {
  bug: 'Bug',
  issue: 'Issue',
}

export default function FindingCard({ finding, screenshotUrl }: Props) {
  return (
    <article className={`finding-card finding-${finding.finding_type}`}>
      <header className="finding-header">
        <span className={`finding-badge finding-badge-${finding.finding_type}`}>
          {TYPE_LABELS[finding.finding_type] ?? finding.finding_type}
        </span>
        <span className={`finding-severity finding-severity-${finding.severity}`}>
          {finding.severity}
        </span>
        <h4 className="finding-title">{finding.title}</h4>
      </header>

      <dl className="finding-body">
        <dt>Steps to reproduce</dt>
        <dd>
          <ol className="finding-steps">
            {finding.steps_to_reproduce
              .split('\n')
              .filter((step) => step.trim().length > 0)
              .map((step, index) => (
                <li key={index}>{step}</li>
              ))}
          </ol>
        </dd>
        <dt>Expected</dt>
        <dd>{finding.expected}</dd>
        <dt>Actual</dt>
        <dd>{finding.actual}</dd>
        {/* Omitted entirely rather than shown empty — findings recorded
            before capture existed have none, and a blank label reads as a
            missing value rather than an older record. */}
        {finding.environment && (
          <>
            <dt>Environment</dt>
            <dd className="finding-environment">{finding.environment}</dd>
          </>
        )}
      </dl>

      {/* The issue-tracker receipt. Absent on a finding that was never
          filed — the run's toggle was off, or the run did not reach the
          completion path — which is a normal state, not a failure, so the
          section disappears rather than reading "not filed". */}
      {finding.tracker_issue_url && (
        <p className="finding-tracker">
          <a href={finding.tracker_issue_url} target="_blank" rel="noreferrer">
            {finding.tracker_issue_key} ↗
          </a>
          {/* Named so the reader knows this finding did not get its own
              ticket, rather than wondering why several cards link to one. */}
          {finding.tracker_is_duplicate && (
            <span className="finding-tracker-grouped"> (grouped)</span>
          )}
        </p>
      )}
      {finding.tracker_error && !finding.tracker_issue_url && (
        <p className="finding-tracker finding-tracker-error">
          Could not file this finding: {finding.tracker_error}
        </p>
      )}

      {/* No screenshot is the normal case when STORE_OFFLINE is disabled,
          and always the case for a scripted finding — the card must read
          cleanly without one rather than showing a broken image. */}
      {screenshotUrl && (
        <a
          className="finding-screenshot-link"
          href={screenshotUrl}
          target="_blank"
          rel="noreferrer"
        >
          <img
            className="finding-screenshot"
            src={screenshotUrl}
            alt={`Screenshot taken when "${finding.title}" was recorded`}
          />
        </a>
      )}
    </article>
  )
}
