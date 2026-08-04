import { useState } from 'react'
import { scriptDownloadUrl } from '../services/api'
import type { TestCaseExecutionResponse } from '../types'
import FindingCard from './FindingCard'
import './TestCaseExecutionRow.css'

const STATUS_LABELS: Record<string, string> = {
  pending: 'Queued',
  running: 'Running',
  passed: 'Passed',
  failed: 'Application bug found',
  error: 'Could not determine — script may still be broken',
  skipped: 'Not run',
}

interface Props {
  caseExecution: TestCaseExecutionResponse
}

export default function TestCaseExecutionRow({ caseExecution }: Props) {
  const [expanded, setExpanded] = useState(false)
  const { status } = caseExecution
  // script_snapshot is only guaranteed set once a case has finalized —
  // showing the download link earlier (pending/running) would 404.
  const finalized = status === 'passed' || status === 'failed' || status === 'error'
  // A skipped case never ran, so its `error` is a one-line explanation
  // rather than script output — shown inline, since "why didn't this run"
  // is the whole question a reader has and must not sit behind a toggle
  // labelled "Show output".
  const skipped = status === 'skipped'
  const hasOutput = !skipped && Boolean(caseExecution.output || caseExecution.error)

  return (
    <li className={`case-execution-row case-execution-row-${status}`}>
      <div className="case-execution-header">
        <span className="case-execution-title">{caseExecution.test_case.title}</span>
        <div className="case-execution-badges">
          {status === 'running' && <span className="case-execution-spinner" aria-hidden="true" />}
          <span className={`case-badge case-badge-${status}`}>
            {STATUS_LABELS[status] ?? status}
          </span>
        </div>
      </div>

      {caseExecution.attempts > 0 && (
        <p className="case-execution-attempts">
          {caseExecution.attempts} attempt{caseExecution.attempts === 1 ? '' : 's'}
        </p>
      )}

      {/* The same card the exploratory pages use — a bug found by a script
          and one found by a session are the same kind of thing, and should
          not need two reading habits. Raw output stays below, collapsed. */}
      {caseExecution.finding && (
        <div className="case-execution-finding">
          <FindingCard finding={caseExecution.finding} />
        </div>
      )}

      {skipped && caseExecution.error && (
        <p className="case-execution-skipped-reason">{caseExecution.error}</p>
      )}

      {hasOutput && (
        <div className="case-execution-output">
          <button className="btn-link" onClick={() => setExpanded((prev) => !prev)} type="button">
            {expanded ? 'Hide output' : 'Show output'}
          </button>
          {expanded && (
            <>
              {caseExecution.error && (
                <pre className="case-execution-error">{caseExecution.error}</pre>
              )}
              {caseExecution.output && (
                <pre className="case-execution-stdout">{caseExecution.output}</pre>
              )}
            </>
          )}
        </div>
      )}

      {finalized && (
        <a
          className="btn-link case-execution-download"
          href={scriptDownloadUrl(caseExecution.id)}
          download
        >
          Download script
        </a>
      )}
    </li>
  )
}
