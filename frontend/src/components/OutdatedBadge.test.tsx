import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import OutdatedBadge from './OutdatedBadge'
import { isOutdated } from '../outdated'

describe('OutdatedBadge', () => {
  it('renders nothing for a current run', () => {
    const { container } = render(
      <OutdatedBadge run={{ outdated_reasons: [], requirement_deleted: false }} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('names each artifact that changed', () => {
    render(
      <OutdatedBadge
        run={{
          outdated_reasons: ['requirement', 'test_plan', 'test_environment'],
          requirement_deleted: false,
        }}
      />,
    )

    expect(screen.getByText('Requirement changed')).toBeInTheDocument()
    expect(screen.getByText('Test plan changed')).toBeInTheDocument()
    expect(screen.getByText('Environment changed')).toBeInTheDocument()
  })

  it('says "deleted" rather than "changed" when the requirement is gone', () => {
    render(<OutdatedBadge run={{ outdated_reasons: ['requirement'], requirement_deleted: true }} />)

    expect(screen.getByText('Requirement deleted')).toBeInTheDocument()
    expect(screen.queryByText('Requirement changed')).not.toBeInTheDocument()
  })

  it('ignores the deleted flag for other reasons', () => {
    render(<OutdatedBadge run={{ outdated_reasons: ['test_plan'], requirement_deleted: true }} />)

    expect(screen.getByText('Test plan changed')).toBeInTheDocument()
  })
})

describe('isOutdated', () => {
  it('derives the boolean from the reasons alone', () => {
    // The backend deliberately ships no `outdated` field — this is the one
    // place the derivation lives, so three call sites cannot disagree.
    expect(isOutdated({ outdated_reasons: [], requirement_deleted: false })).toBe(false)
    expect(isOutdated({ outdated_reasons: ['requirement'], requirement_deleted: true })).toBe(true)
  })
})
