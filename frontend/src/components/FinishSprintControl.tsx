import { useState } from 'react'
import FinishSprintModal from './FinishSprintModal'
import { finishSprint } from '../services/api'
import type { SprintResponse } from '../types'

/**
 * Finish Sprint, in the same place on every page inside a sprint: under the
 * breadcrumb, above the back/next row.
 *
 * It used to sit at the *bottom* of three of the seven sprint pages and nowhere
 * on the other four — so on a long run page the only way to close a sprint was
 * to scroll past everything, and from a session sheet there was no way at all.
 *
 * The button, the dialog, and the busy and error state were written out three
 * times before this; they live here once. The error deliberately goes to the
 * dialog rather than the page, because the page's own error state blanks the
 * page and the user is looking at the dialog.
 */

interface Props {
  /** `null` while the page is still loading it. Renders nothing until it lands. */
  sprint: SprintResponse | null | undefined
  /** Hand the finished sprint back, so the page's copy does not go stale. */
  onFinished: (sprint: SprintResponse) => void
}

export default function FinishSprintControl({ sprint, onFinished }: Props) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // A finished sprint stays readable — there is just nothing left to finish.
  if (!sprint?.active) return null

  const handleFinish = () => {
    setBusy(true)
    setError(null)
    finishSprint(sprint.id)
      .then((updated) => {
        onFinished(updated)
        setConfirming(false)
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setBusy(false))
  }

  return (
    <>
      <div className="sprint-actions">
        <button
          className="btn btn-small btn-danger"
          disabled={busy}
          onClick={() => setConfirming(true)}
        >
          Finish Sprint
        </button>
      </div>

      {confirming && (
        <FinishSprintModal
          sprintName={sprint.name}
          busy={busy}
          error={error}
          onConfirm={handleFinish}
          onCancel={() => {
            setConfirming(false)
            setError(null)
          }}
        />
      )}
    </>
  )
}
