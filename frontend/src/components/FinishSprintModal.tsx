import ConfirmModal from './ConfirmModal'

/**
 * Finishing a sprint is the one irreversible action in the app: the API accepts
 * only the active -> finished transition, so there is no way back. It also
 * stops work in flight, failing any in-progress rows and abandoning their
 * unreached children.
 *
 * The copy lives here rather than at each of the four call sites, so all four
 * say the same thing about what is about to happen.
 */

interface Props {
  sprintName: string
  busy?: boolean
  error?: string | null
  onConfirm: () => void
  onCancel: () => void
}

export default function FinishSprintModal({
  sprintName,
  busy = false,
  error = null,
  onConfirm,
  onCancel,
}: Props) {
  return (
    <ConfirmModal
      title="Finish this sprint?"
      confirmLabel="Finish sprint"
      busyLabel="Finishing…"
      busy={busy}
      error={error}
      onConfirm={onConfirm}
      onCancel={onCancel}
      body={
        <>
          <p>
            <strong>{sprintName}</strong> will be closed, and a finished sprint cannot be reopened.
          </p>
          <p>
            Any requirement analysis, test run or exploratory session still in progress will be
            stopped.
          </p>
        </>
      }
    />
  )
}
