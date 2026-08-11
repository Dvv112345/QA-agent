import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ConfirmModal from './ConfirmModal'

function renderModal(overrides: Partial<Parameters<typeof ConfirmModal>[0]> = {}) {
  const onConfirm = vi.fn()
  const onCancel = vi.fn()
  render(
    <ConfirmModal
      title="Finish this sprint?"
      body={<p>This cannot be undone.</p>}
      confirmLabel="Finish sprint"
      onConfirm={onConfirm}
      onCancel={onCancel}
      {...overrides}
    />,
  )
  return { onConfirm, onCancel }
}

describe('ConfirmModal', () => {
  it('renders the title and body inside a labelled dialog', () => {
    renderModal()

    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    // The heading labels the dialog, so a screen reader announces what it is.
    expect(dialog).toHaveAccessibleName('Finish this sprint?')
    expect(screen.getByText('This cannot be undone.')).toBeInTheDocument()
  })

  it('confirms and cancels through their buttons', () => {
    const { onConfirm, onCancel } = renderModal()

    fireEvent.click(screen.getByRole('button', { name: 'Finish sprint' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('cancels on Escape', () => {
    const { onCancel } = renderModal()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('cancels on a backdrop click but not on a click inside the card', () => {
    const { onCancel } = renderModal()

    fireEvent.click(screen.getByRole('dialog'))
    expect(onCancel).not.toHaveBeenCalled()

    // The overlay is the dialog's parent; only a click landing on it dismisses.
    fireEvent.click(screen.getByRole('dialog').parentElement as HTMLElement)
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('blocks every dismissal path while busy', () => {
    const { onCancel } = renderModal({ busy: true, busyLabel: 'Finishing…' })

    expect(screen.getByRole('button', { name: 'Finishing…' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()

    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(screen.getByRole('dialog').parentElement as HTMLElement)

    // A request in flight must not be abandoned halfway.
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('moves focus into the dialog and restores it on close', () => {
    const trigger = document.createElement('button')
    document.body.appendChild(trigger)
    trigger.focus()
    expect(document.activeElement).toBe(trigger)

    const { unmount } = render(
      <ConfirmModal
        title="Finish this sprint?"
        body={<p>Body</p>}
        confirmLabel="Finish sprint"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: 'Finish sprint' })).toHaveFocus()

    unmount()
    expect(document.activeElement).toBe(trigger)
    trigger.remove()
  })

  it('shows an error without dismissing', () => {
    renderModal({ error: 'Sprint already finished.' })

    expect(screen.getByText('Sprint already finished.')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})
