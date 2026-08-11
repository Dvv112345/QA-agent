import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderWithRouter } from '../test/test-utils'
import StageNav from './StageNav'

describe('StageNav', () => {
  it('links to the next stage when the gate is open', () => {
    renderWithRouter(
      <StageNav
        to="/sprints/1/test-plans"
        label="Test Plans"
        ready
        blockedReason="Confirm the test environment to continue."
      />,
    )

    expect(screen.getByRole('link', { name: /Test Plans/ })).toHaveAttribute(
      'href',
      '/sprints/1/test-plans',
    )
    // The reason belongs to the shut state only.
    expect(screen.queryByText('Confirm the test environment to continue.')).not.toBeInTheDocument()
  })

  it('states the reason instead of hiding when the gate is shut', () => {
    renderWithRouter(
      <StageNav
        to="/sprints/1/test-plans"
        label="Test Plans"
        ready={false}
        blockedReason="Confirm the test environment to continue."
      />,
    )

    // Always rendered, so the user can see where they are headed and why they
    // cannot go yet — the old pages hid the control entirely until it worked.
    const button = screen.getByRole('button', { name: /Test Plans/ })
    expect(button).toBeDisabled()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()

    // The reason is attached to the control, not merely printed beside it: a
    // disabled control that cannot say why is the defect this replaced.
    expect(button).toHaveAccessibleDescription('Confirm the test environment to continue.')
  })
})
