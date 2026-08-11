import { type ReactNode } from 'react'
import ModalShell from './ModalShell'
import './ConfirmModal.css'

/**
 * A confirmation dialog for actions that genuinely cannot be undone.
 *
 * Deliberately not `window.confirm`. The three confirms this app used to show
 * guarded artifacts that turned out to be editable, and they have been removed;
 * the one action that is actually irreversible — finishing a sprint — had none
 * at all. A styled dialog draws the distinction the old UI failed to draw.
 */

interface Props {
  title: string
  body: ReactNode
  confirmLabel: string
  /** `danger` for irreversible actions; `primary` otherwise. */
  confirmVariant?: 'primary' | 'danger'
  busyLabel?: string
  busy?: boolean
  error?: string | null
  onConfirm: () => void
  onCancel: () => void
}

export default function ConfirmModal({
  title,
  body,
  confirmLabel,
  confirmVariant = 'danger',
  busyLabel = 'Working…',
  busy = false,
  error = null,
  onConfirm,
  onCancel,
}: Props) {
  return (
    <ModalShell title={title} busy={busy} onClose={onCancel}>
      <div className="confirm-modal-body">{body}</div>

      {error && <p className="confirm-modal-error">{error}</p>}

      <div className="confirm-modal-actions">
        <button className={`btn btn-${confirmVariant}`} onClick={onConfirm} disabled={busy}>
          {busy ? busyLabel : confirmLabel}
        </button>
        <button className="btn btn-secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </ModalShell>
  )
}
