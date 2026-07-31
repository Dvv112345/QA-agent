import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import TestCaseExecutionRow from './TestCaseExecutionRow'
import type { Finding, TestCaseExecutionResponse } from '../types'

function makeFinding(overrides: Partial<Finding> = {}): Finding {
  return {
    finding_type: 'bug',
    severity: 'high',
    title: 'Valid credentials are rejected',
    steps_to_reproduce: 'Open /login\nSubmit valid credentials',
    expected: 'The user reaches the dashboard',
    actual: 'A 401 is returned',
    environment: 'Windows-10 · Python 3.12.4',
    ...overrides,
  }
}

function makeCase(overrides: Partial<TestCaseExecutionResponse> = {}): TestCaseExecutionResponse {
  return {
    id: 3,
    test_case: {
      id: 11,
      position: 0,
      title: 'Valid login',
      preconditions: null,
      steps: 'Open the login page',
      expected_result: 'User lands on the dashboard.',
      case_type: 'functional',
      priority: 'high',
    },
    status: 'passed',
    attempts: 1,
    output: null,
    error: null,
    finding: null,
    updated_at: '2026-07-31T00:00:00Z',
    ...overrides,
  }
}

describe('TestCaseExecutionRow', () => {
  it('shows the finding card for a failed case', () => {
    render(
      <TestCaseExecutionRow
        caseExecution={makeCase({ status: 'failed', finding: makeFinding() })}
      />,
    )

    expect(screen.getByText('Bug')).toBeInTheDocument()
    expect(screen.getByText('Valid credentials are rejected')).toBeInTheDocument()
    expect(screen.getByText('The user reaches the dashboard')).toBeInTheDocument()
    expect(screen.getByText('Windows-10 · Python 3.12.4')).toBeInTheDocument()
  })

  it('reports an exhausted self-heal as an issue, not a bug', () => {
    render(
      <TestCaseExecutionRow
        caseExecution={makeCase({
          status: 'error',
          finding: makeFinding({ finding_type: 'issue', severity: 'medium' }),
        })}
      />,
    )

    expect(screen.getByText('Issue')).toBeInTheDocument()
    expect(screen.queryByText('Bug')).not.toBeInTheDocument()
  })

  it('shows no finding card for a passing case', () => {
    render(<TestCaseExecutionRow caseExecution={makeCase()} />)

    expect(screen.queryByText('Bug')).not.toBeInTheDocument()
    expect(screen.queryByText('Steps to reproduce')).not.toBeInTheDocument()
  })

  it('keeps the raw output behind its toggle alongside the card', () => {
    render(
      <TestCaseExecutionRow
        caseExecution={makeCase({
          status: 'failed',
          finding: makeFinding(),
          output: 'AssertionError: expected 200',
        })}
      />,
    )

    // The card is the report; the output is the debugging surface, and a
    // reader wants both without one hiding the other.
    expect(screen.getByText('Valid credentials are rejected')).toBeInTheDocument()
    expect(screen.queryByText('AssertionError: expected 200')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Show output' }))
    expect(screen.getByText('AssertionError: expected 200')).toBeInTheDocument()
  })

  it('offers the script download once the case has finalized', () => {
    render(
      <TestCaseExecutionRow
        caseExecution={makeCase({ status: 'failed', finding: makeFinding() })}
      />,
    )

    expect(screen.getByRole('link', { name: 'Download script' })).toBeInTheDocument()
  })

  it('hides the script download while the case is still running', () => {
    render(<TestCaseExecutionRow caseExecution={makeCase({ status: 'running', attempts: 0 })} />)

    expect(screen.queryByRole('link', { name: 'Download script' })).not.toBeInTheDocument()
  })
})
