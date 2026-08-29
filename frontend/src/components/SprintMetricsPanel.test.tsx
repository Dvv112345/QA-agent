import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import SprintMetricsPanel from './SprintMetricsPanel'
import type { RequirementMetrics, SprintMetrics } from '../types'

function makeRow(overrides: Partial<RequirementMetrics> = {}): RequirementMetrics {
  return {
    requirement_id: 1,
    requirement_name: 'Checkout',
    requirement_deleted: false,
    bug_count: 0,
    issue_count: 0,
    distinct_test_cases_run: 0,
    exploratory_sessions: 0,
    ...overrides,
  }
}

function makeMetrics(overrides: Partial<SprintMetrics> = {}): SprintMetrics {
  return {
    sprint_id: 1,
    distinct_test_cases_run: 20,
    case_executions: 60,
    executions_passed: 52,
    executions_failed: 6,
    executions_errored: 2,
    exploratory_sessions: 8,
    requirements_explored: 3,
    urls_examined: 12,
    requirements_examined: 2,
    bug_count: 9,
    functional_bug_count: 7,
    nonfunctional_bug_count: 2,
    bugs_by_domain: { accessibility: 2 },
    issue_count: 2,
    high_severity_bug_count: 4,
    requirements_covered: 5,
    requirements_total: 7,
    bugs_per_requirement: 1.8,
    bugs_per_test_case: 0.45,
    per_requirement: [],
    excluded_runs_running: 0,
    excluded_runs_failed: 0,
    ...overrides,
  }
}

describe('SprintMetricsPanel', () => {
  it('renders both tests-run tiles at their separate counting levels', () => {
    render(<SprintMetricsPanel metrics={makeMetrics()} />)

    // The headline is the distinct count; executions sit beneath it,
    // labelled, so the two are never mistaken for each other.
    expect(screen.getByText('20')).toBeInTheDocument()
    expect(screen.getByText('60 executions')).toBeInTheDocument()
    expect(screen.getByText('52 passed · 6 failed · 2 errored')).toBeInTheDocument()

    // Exploratory sessions are reported separately, never summed in.
    expect(screen.getByText('8')).toBeInTheDocument()
    expect(screen.getByText('3 requirements explored')).toBeInTheDocument()
  })

  it('renders the bug counts and both densities', () => {
    render(<SprintMetricsPanel metrics={makeMetrics()} />)

    expect(screen.getByText('9')).toBeInTheDocument()
    expect(screen.getByText('4 high severity')).toBeInTheDocument()
    expect(screen.getByText('2 issues')).toBeInTheDocument()
    expect(screen.getByText('1.80')).toBeInTheDocument()
    expect(screen.getByText('0.45 bugs / case')).toBeInTheDocument()
    expect(screen.getByText('5 requirements covered')).toBeInTheDocument()
    expect(screen.getByText('7 current requirements')).toBeInTheDocument()
  })

  it('renders an em dash rather than a zero for a null density', () => {
    render(
      <SprintMetricsPanel
        metrics={makeMetrics({
          distinct_test_cases_run: 0,
          requirements_covered: 0,
          bugs_per_requirement: null,
          bugs_per_test_case: null,
        })}
      />,
    )

    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.getByText('— bugs / case')).toBeInTheDocument()
  })

  it('renders < 0.01 rather than 0.00 for a genuinely non-zero ratio', () => {
    // The one output this tile must never produce: "0.00 bugs / case" on a
    // sprint that actually found bugs.
    render(<SprintMetricsPanel metrics={makeMetrics({ bugs_per_test_case: 0.002 })} />)

    expect(screen.getByText('< 0.01 bugs / case')).toBeInTheDocument()
    expect(screen.queryByText('0.00 bugs / case')).not.toBeInTheDocument()
  })

  it('renders 0.00 when a sprint was tested and is clean', () => {
    render(<SprintMetricsPanel metrics={makeMetrics({ bug_count: 0, bugs_per_test_case: 0 })} />)

    expect(screen.getByText('0.00 bugs / case')).toBeInTheDocument()
  })

  it('names the excluded runs when some were left out', () => {
    render(
      <SprintMetricsPanel
        metrics={makeMetrics({ excluded_runs_running: 1, excluded_runs_failed: 2 })}
      />,
    )

    const notice = screen.getByText(/3 runs excluded/)
    expect(notice).toHaveTextContent('1 running')
    expect(notice).toHaveTextContent('2 failed')
  })

  it('hides the exclusion notice when every run was counted', () => {
    render(<SprintMetricsPanel metrics={makeMetrics()} />)

    expect(screen.queryByText(/excluded/)).not.toBeInTheDocument()
  })

  it('renders the per-requirement rows', () => {
    render(
      <SprintMetricsPanel
        metrics={makeMetrics({
          bug_count: 5,
          per_requirement: [
            makeRow({ requirement_id: 1, requirement_name: 'Checkout', bug_count: 3 }),
            makeRow({
              requirement_id: 2,
              requirement_name: 'Login',
              bug_count: 2,
              exploratory_sessions: 4,
            }),
          ],
        })}
      />,
    )

    expect(screen.getByText('Checkout')).toBeInTheDocument()
    expect(screen.getByText('Login')).toBeInTheDocument()
    expect(screen.getByText('4')).toBeInTheDocument()
  })

  it('marks an archived requirement as deleted', () => {
    render(
      <SprintMetricsPanel
        metrics={makeMetrics({
          per_requirement: [makeRow({ requirement_name: 'Removed', requirement_deleted: true })],
        })}
      />,
    )

    expect(screen.getByText('(deleted)')).toBeInTheDocument()
  })

  it('footnotes cross-requirement defects only when the rows sum above the headline', () => {
    render(
      <SprintMetricsPanel
        metrics={makeMetrics({
          bug_count: 1,
          per_requirement: [
            makeRow({ requirement_id: 1, requirement_name: 'Checkout', bug_count: 1 }),
            makeRow({ requirement_id: 2, requirement_name: 'Login', bug_count: 1 }),
          ],
        })}
      />,
    )

    expect(screen.getByText(/one defect can affect several requirements/)).toBeInTheDocument()
  })

  it('hides the footnote when the rows and the headline agree', () => {
    render(
      <SprintMetricsPanel
        metrics={makeMetrics({
          bug_count: 2,
          per_requirement: [
            makeRow({ requirement_id: 1, requirement_name: 'Checkout', bug_count: 1 }),
            makeRow({ requirement_id: 2, requirement_name: 'Login', bug_count: 1 }),
          ],
        })}
      />,
    )

    expect(screen.queryByText(/one defect can affect several requirements/)).not.toBeInTheDocument()
  })
})
