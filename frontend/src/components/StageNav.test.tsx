import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderWithRouter } from '../test/test-utils'
import type { SprintResponse } from '../types'
import StageNav from './StageNav'

/** Only the gate flags matter here; the rest of the row is never read. */
function sprintWith(flags: Partial<SprintResponse>): SprintResponse {
  return {
    requirements_complete: false,
    environment_confirmed: false,
    test_plans_complete: false,
    ...flags,
  } as SprintResponse
}

describe('StageNav', () => {
  it('links to the next stage when the gate is open', () => {
    renderWithRouter(
      <StageNav
        stage="test-plans"
        sprintId={1}
        sprint={sprintWith({ environment_confirmed: true })}
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
    renderWithRouter(<StageNav stage="test-plans" sprintId={1} sprint={sprintWith({})} />)

    // Always rendered, so the user can see where they are headed and why they
    // cannot go yet — the old pages hid the control entirely until it worked.
    const button = screen.getByRole('button', { name: /Test Plans/ })
    expect(button).toBeDisabled()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()

    // The reason is attached to the control, not merely printed beside it: a
    // disabled control that cannot say why is the defect this replaced.
    expect(button).toHaveAccessibleDescription('Confirm the test environment to continue.')
  })

  it('reads its label, gate and reason from the stage it is given', () => {
    // The same component, a different stage — nothing about Test Runs is spelled
    // out at the call site, so it cannot drift from the breadcrumb's copy.
    renderWithRouter(<StageNav stage="test-runs" sprintId={4} sprint={sprintWith({})} />)

    expect(screen.getByRole('button', { name: /Test Runs/ })).toHaveAccessibleDescription(
      'Approve every test plan to continue.',
    )
  })
})
