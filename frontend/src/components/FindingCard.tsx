import { findingScreenshotUrl } from '../services/api'
import type { ExploratoryFindingResponse } from '../types'
import './FindingCard.css'

interface Props {
  finding: ExploratoryFindingResponse
}

const TYPE_LABELS: Record<string, string> = {
  bug: 'Bug',
  issue: 'Issue',
}

export default function FindingCard({ finding }: Props) {
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
      </dl>

      {/* No screenshot is the normal case when STORE_OFFLINE is disabled —
          the card must read cleanly without one rather than showing a
          broken image. */}
      {finding.has_screenshot && (
        <a
          className="finding-screenshot-link"
          href={findingScreenshotUrl(finding.id)}
          target="_blank"
          rel="noreferrer"
        >
          <img
            className="finding-screenshot"
            src={findingScreenshotUrl(finding.id)}
            alt={`Screenshot taken when "${finding.title}" was recorded`}
          />
        </a>
      )}
    </article>
  )
}
