import type { SprintMetrics } from '../types'
import './SprintMetricsPanel.css'

interface Props {
  /**
   * Every figure is computed server-side (`services/qa_metrics.py`).
   * Nothing here recomputes one from the others — see `SprintMetrics`.
   */
  metrics: SprintMetrics
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? '' : 's'}`
}

/**
 * A density figure in the unit it is actually computed in — bugs per
 * requirement, bugs per case — never rescaled to a per-100 figure, so
 * nobody has to work out whether a "5" is five bugs or five percent.
 *
 * Three states that must read distinctly:
 *
 * - `—` for null: nothing was tested, so there is no ratio to report.
 * - `< 0.01` for a value that is genuinely non-zero but rounds to zero at
 *   2 dp (a sprint past ~200 cases per bug). Rendering `0.00` on a sprint
 *   that found bugs is the one output this must never produce.
 * - the rounded number otherwise, `0.00` included — that one is honest,
 *   because it means tested and clean.
 */
function formatDensity(value: number | null): string {
  if (value === null) return '—'
  if (value > 0 && value < 0.005) return '< 0.01'
  return value.toFixed(2)
}

export default function SprintMetricsPanel({ metrics }: Props) {
  const {
    distinct_test_cases_run: distinctCases,
    case_executions: executions,
    executions_passed: passed,
    executions_failed: failed,
    executions_errored: errored,
    exploratory_sessions: sessions,
    requirements_explored: explored,
    bug_count: bugs,
    issue_count: issues,
    high_severity_bug_count: highSeverity,
    requirements_covered: covered,
    requirements_total: total,
    bugs_per_requirement: perRequirement,
    bugs_per_test_case: perCase,
    per_requirement: rows,
    excluded_runs_running: excludedRunning,
    excluded_runs_failed: excludedFailed,
  } = metrics

  const excluded = excludedRunning + excludedFailed
  // Grouping is sprint-scoped, so one ticket can span two requirements and
  // the rows can legitimately sum above the headline. Footnoted only when
  // they actually differ — a sprint with no cross-cutting defect, which is
  // the common case, never sees the sentence.
  const rowBugTotal = rows.reduce((sum, row) => sum + row.bug_count, 0)

  return (
    <section className="sprint-metrics">
      <h2 className="sprint-metrics-heading">QA Metrics</h2>

      <div className="sprint-metrics-tiles">
        {/* Two "tests run" tiles, deliberately never summed: a 25-action
            browser session and a 3-step script are not the same unit. */}
        <div className="sprint-metrics-tile">
          <span className="sprint-metrics-tile-label">Test cases run</span>
          <span className="sprint-metrics-tile-value">{distinctCases}</span>
          <span className="sprint-metrics-tile-unit">distinct</span>
          {/* The second counting level, labelled so the two are never
              mistaken for each other. */}
          <span className="sprint-metrics-tile-detail">{plural(executions, 'execution')}</span>
          {executions > 0 && (
            <span className="sprint-metrics-tile-detail">
              {passed} passed · {failed} failed · {errored} errored
            </span>
          )}
        </div>

        <div className="sprint-metrics-tile">
          <span className="sprint-metrics-tile-label">Exploratory</span>
          <span className="sprint-metrics-tile-value">{sessions}</span>
          <span className="sprint-metrics-tile-unit">sessions</span>
          <span className="sprint-metrics-tile-detail">
            {plural(explored, 'requirement')} explored
          </span>
        </div>

        <div className="sprint-metrics-tile">
          <span className="sprint-metrics-tile-label">Bugs</span>
          <span className="sprint-metrics-tile-value">{bugs}</span>
          <span className="sprint-metrics-tile-unit">distinct</span>
          <span className="sprint-metrics-tile-detail">{highSeverity} high severity</span>
          <span className="sprint-metrics-tile-detail">{plural(issues, 'issue')}</span>
        </div>

        <div className="sprint-metrics-tile">
          <span className="sprint-metrics-tile-label">Defect density</span>
          <span className="sprint-metrics-tile-value">{formatDensity(perRequirement)}</span>
          <span className="sprint-metrics-tile-unit">bugs / requirement</span>
          <span className="sprint-metrics-tile-detail">{formatDensity(perCase)} bugs / case</span>
          {/* Coverage sits beside the ratio rather than inside it: the
              denominator above is what was actually exercised, so this is
              the only place the untested remainder is visible.

              Two facts, never a fraction. `covered` counts requirements a
              counted run touched — archived ones included — while `total`
              counts live confirmed ones, so editing or deleting a covered
              requirement legitimately puts covered above total. Phrased as
              "1 of 0 requirements covered" that reads as a broken panel and
              costs the reader's trust in the tiles beside it. "currently"
              is what makes the odd case self-explaining: the total is a
              live snapshot, the coverage is what runs already did. */}
          <span className="sprint-metrics-tile-detail">
            {' '}
            {plural(covered, 'requirement')} covered{' '}
          </span>
          <span className="sprint-metrics-tile-detail">
            {total} current {plural(total, 'requirement')}
          </span>
        </div>
      </div>

      {excluded > 0 && (
        <p className="sprint-metrics-excluded">
          ⚠ {plural(excluded, 'run')} excluded (
          {[
            excludedRunning > 0 ? `${excludedRunning} running` : null,
            excludedFailed > 0 ? `${excludedFailed} failed` : null,
          ]
            .filter(Boolean)
            .join(', ')}
          ) — only completed runs are counted.
        </p>
      )}

      {rows.length > 0 && (
        <div className="sprint-metrics-table-wrapper">
          <table className="sprint-metrics-table">
            <thead>
              <tr>
                <th>Requirement</th>
                <th>Bugs</th>
                <th>Issues</th>
                <th>Cases</th>
                <th>Sessions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.requirement_id}>
                  <td>
                    {row.requirement_name}
                    {row.requirement_deleted && (
                      <span className="sprint-metrics-deleted"> (deleted)</span>
                    )}
                  </td>
                  <td>{row.bug_count}</td>
                  <td>{row.issue_count}</td>
                  <td>{row.distinct_test_cases_run}</td>
                  <td>{row.exploratory_sessions}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {rowBugTotal > bugs && (
        <p className="sprint-metrics-footnote">
          Rows sum above the headline because one defect can affect several requirements; it is
          counted once overall and once per requirement it touches.
        </p>
      )}
    </section>
  )
}
