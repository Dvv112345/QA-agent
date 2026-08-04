import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import FindingCard from './FindingCard'
import type { Finding } from '../types'

function makeFinding(overrides: Partial<Finding> = {}): Finding {
  return {
    finding_type: 'bug',
    severity: 'high',
    title: 'Empty export omits the header row',
    steps_to_reproduce: 'Open reports\nFilter to zero rows\nClick Export',
    expected: 'A CSV containing a header row',
    actual: 'A zero-byte file',
    environment: null,
    tracker_issue_key: null,
    tracker_issue_url: null,
    tracker_error: null,
    tracker_is_duplicate: false,
    ...overrides,
  }
}

describe('FindingCard', () => {
  it('renders the title, severity, and expected vs actual', () => {
    render(<FindingCard finding={makeFinding()} />)

    expect(screen.getByText('Empty export omits the header row')).toBeInTheDocument()
    expect(screen.getByText('high')).toBeInTheDocument()
    expect(screen.getByText('A CSV containing a header row')).toBeInTheDocument()
    expect(screen.getByText('A zero-byte file')).toBeInTheDocument()
  })

  it('splits reproduction steps into a list', () => {
    render(<FindingCard finding={makeFinding()} />)

    const steps = screen.getAllByRole('listitem')
    expect(steps).toHaveLength(3)
    expect(steps[0]).toHaveTextContent('Open reports')
    expect(steps[2]).toHaveTextContent('Click Export')
  })

  it('distinguishes an issue from a bug', () => {
    render(<FindingCard finding={makeFinding({ finding_type: 'issue' })} />)
    expect(screen.getByText('Issue')).toBeInTheDocument()
    expect(screen.queryByText('Bug')).not.toBeInTheDocument()
  })

  it('shows where the finding was observed', () => {
    render(
      <FindingCard
        finding={makeFinding({
          environment: 'Chromium 131 · viewport 1280x720 · https://app.test/checkout',
        })}
      />,
    )

    expect(screen.getByText('Environment')).toBeInTheDocument()
    expect(
      screen.getByText('Chromium 131 · viewport 1280x720 · https://app.test/checkout'),
    ).toBeInTheDocument()
  })

  it('omits the environment row entirely when there is none', () => {
    // Findings recorded before capture existed have none — a blank label
    // would read as a missing value rather than an older record.
    render(<FindingCard finding={makeFinding({ environment: null })} />)
    expect(screen.queryByText('Environment')).not.toBeInTheDocument()
  })

  it('renders cleanly without a screenshot', () => {
    // The normal case when STORE_OFFLINE is disabled, and always the case
    // for a scripted finding — no broken image.
    render(<FindingCard finding={makeFinding()} />)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  it('shows the screenshot the caller supplies', () => {
    render(
      <FindingCard
        finding={makeFinding()}
        screenshotUrl="/api/exploratory-findings/7/screenshot"
      />,
    )

    const image = screen.getByRole('img')
    expect(image).toHaveAttribute('src', '/api/exploratory-findings/7/screenshot')
  })

  it('links the filed ticket', () => {
    render(
      <FindingCard
        finding={makeFinding({
          tracker_issue_key: 'QA-142',
          tracker_issue_url: 'https://acme.atlassian.net/browse/QA-142',
        })}
      />,
    )

    const link = screen.getByRole('link', { name: /QA-142/ })
    expect(link).toHaveAttribute('href', 'https://acme.atlassian.net/browse/QA-142')
  })

  it('marks a finding grouped into another ticket', () => {
    // Without this the reader wonders why several cards link to one issue.
    render(
      <FindingCard
        finding={makeFinding({
          tracker_issue_key: 'QA-142',
          tracker_issue_url: 'https://acme.atlassian.net/browse/QA-142',
          tracker_is_duplicate: true,
        })}
      />,
    )

    expect(screen.getByText('(grouped)')).toBeInTheDocument()
  })

  it('does not mark the ticket-owning finding as grouped', () => {
    render(
      <FindingCard
        finding={makeFinding({
          tracker_issue_key: 'QA-142',
          tracker_issue_url: 'https://acme.atlassian.net/browse/QA-142',
        })}
      />,
    )

    expect(screen.queryByText('(grouped)')).not.toBeInTheDocument()
  })

  it('reports a filing failure inline', () => {
    render(
      <FindingCard finding={makeFinding({ tracker_error: 'Jira rejected the request (403)' })} />,
    )

    expect(screen.getByText(/Jira rejected the request \(403\)/)).toBeInTheDocument()
  })

  it('shows no tracker section when the finding was never filed', () => {
    // The toggle was off, or the run never reached the completion path —
    // a normal state, not a failure to announce.
    render(<FindingCard finding={makeFinding()} />)

    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.queryByText(/Could not file/)).not.toBeInTheDocument()
  })

  it('prefers the link over a stale error', () => {
    // A retry that succeeded writes the key and clears the error, but a
    // card must never show both even if one lingers.
    render(
      <FindingCard
        finding={makeFinding({
          tracker_issue_key: 'QA-142',
          tracker_issue_url: 'https://acme.atlassian.net/browse/QA-142',
          tracker_error: 'an older failure',
        })}
      />,
    )

    expect(screen.getByRole('link', { name: /QA-142/ })).toBeInTheDocument()
    expect(screen.queryByText(/Could not file/)).not.toBeInTheDocument()
  })
})
