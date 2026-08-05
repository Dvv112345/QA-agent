import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import ExportSummary from './ExportSummary'
import type { ExportRollup } from '../types'

function makeRollup(overrides: Partial<ExportRollup> = {}): ExportRollup {
  return {
    export_findings: false,
    exported_finding_count: 0,
    exported_issue_count: 0,
    export_error_count: 0,
    unexported_finding_count: 0,
    export_groups: [],
    ...overrides,
  }
}

function renderSummary(rollup: ExportRollup, onExport = vi.fn().mockResolvedValue(undefined)) {
  render(<ExportSummary rollup={rollup} onExport={onExport} />)
  return onExport
}

describe('ExportSummary', () => {
  it('renders nothing for a run with no bug findings', () => {
    const { container } = render(<ExportSummary rollup={makeRollup()} onExport={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('states both totals so grouping reads as grouping', () => {
    // Six findings becoming two issues must not look like four went
    // missing.
    renderSummary(makeRollup({ exported_finding_count: 6, exported_issue_count: 2 }))

    expect(screen.getByText('6 bugs filed as 2 issues')).toBeInTheDocument()
  })

  it('lists each ticket with how many findings it stands for', () => {
    renderSummary(
      makeRollup({
        exported_finding_count: 5,
        exported_issue_count: 2,
        export_groups: [
          { issue_key: 'QA-142', issue_url: 'https://x/QA-142', finding_count: 4 },
          { issue_key: 'QA-143', issue_url: 'https://x/QA-143', finding_count: 1 },
        ],
      }),
    )

    expect(screen.getByRole('link', { name: /QA-142/ })).toHaveAttribute('href', 'https://x/QA-142')
    expect(screen.getByText(/4 findings/)).toBeInTheDocument()
    expect(screen.getByText(/1 finding$/)).toBeInTheDocument()
  })

  it('offers to file a run whose bugs were never filed', () => {
    // The manual path, not a fallback: a run that ended any way other
    // than completed arrives here unfiled by design.
    renderSummary(makeRollup({ unexported_finding_count: 6 }))

    expect(screen.getByRole('button', { name: 'File 6 bugs' })).toBeInTheDocument()
  })

  it('renders an unlinked key when no URL was recorded', () => {
    // The key is what a human quotes, so it must survive a blank URL —
    // and an <a href=""> would reload the page.
    renderSummary(
      makeRollup({
        exported_finding_count: 1,
        exported_issue_count: 1,
        export_groups: [{ issue_key: 'QA-142', issue_url: '', finding_count: 1 }],
      }),
    )

    expect(screen.getByText('QA-142')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('says a run was never set to file, rather than implying it failed', () => {
    renderSummary(makeRollup({ export_findings: false, unexported_finding_count: 6 }))

    expect(
      screen.getByText('This run was not set to file findings automatically.'),
    ).toBeInTheDocument()
    // The button still works — pressing it is the instruction itself.
    expect(screen.getByRole('button', { name: 'File 6 bugs' })).toBeEnabled()
  })

  it('says nothing of the sort when the run was set to file', () => {
    renderSummary(makeRollup({ export_findings: true, unexported_finding_count: 6 }))

    expect(screen.queryByText(/not set to file/)).not.toBeInTheDocument()
  })

  it('does not blame the toggle when filing actually failed', () => {
    renderSummary(
      makeRollup({ export_findings: false, unexported_finding_count: 1, export_error_count: 1 }),
    )

    expect(screen.queryByText(/not set to file/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('says Retry when filing was attempted and failed', () => {
    renderSummary(makeRollup({ unexported_finding_count: 2, export_error_count: 2 }))

    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.getByText('2 findings could not be filed.')).toBeInTheDocument()
  })

  it('offers nothing to press when everything is filed', () => {
    renderSummary(makeRollup({ exported_finding_count: 3, exported_issue_count: 1 }))

    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('calls the export handler', async () => {
    const onExport = renderSummary(makeRollup({ unexported_finding_count: 1 }))

    fireEvent.click(screen.getByRole('button', { name: 'File 1 bug' }))

    await waitFor(() => expect(onExport).toHaveBeenCalled())
  })

  it('surfaces an export failure inline', async () => {
    const onExport = vi.fn().mockRejectedValue(new Error('Connect an issue tracker first.'))
    renderSummary(makeRollup({ unexported_finding_count: 1 }), onExport)

    fireEvent.click(screen.getByRole('button', { name: 'File 1 bug' }))

    expect(await screen.findByText('Connect an issue tracker first.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'File 1 bug' })).toBeEnabled()
  })

  it('shows both halves when some filed and some did not', () => {
    renderSummary(
      makeRollup({
        exported_finding_count: 2,
        exported_issue_count: 1,
        unexported_finding_count: 1,
        export_error_count: 1,
      }),
    )

    expect(screen.getByText('2 bugs filed as 1 issue')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })
})
