import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import FindingCard from './FindingCard'
import type { ExploratoryFindingResponse } from '../types'

function makeFinding(
  overrides: Partial<ExploratoryFindingResponse> = {},
): ExploratoryFindingResponse {
  return {
    id: 7,
    position: 0,
    finding_type: 'bug',
    severity: 'high',
    title: 'Empty export omits the header row',
    steps_to_reproduce: 'Open reports\nFilter to zero rows\nClick Export',
    expected: 'A CSV containing a header row',
    actual: 'A zero-byte file',
    has_screenshot: false,
    created_at: '2026-07-28T00:00:00Z',
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

  it('renders cleanly without a screenshot', () => {
    // The normal case when STORE_OFFLINE is disabled — no broken image.
    render(<FindingCard finding={makeFinding({ has_screenshot: false })} />)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  it('shows the screenshot when one exists', () => {
    render(<FindingCard finding={makeFinding({ has_screenshot: true })} />)

    const image = screen.getByRole('img')
    expect(image).toHaveAttribute('src', expect.stringContaining('/exploratory-findings/7/'))
  })
})
