import { isOutdated } from '../outdated'
import type { OutdatableRun } from '../outdated'

interface Props {
  /** The run or execution being offered a restart. */
  run: OutdatableRun
  /** Whether restarting is available at all — status and sprint gates. */
  enabled: boolean
  busy: boolean
  /** Button text, e.g. `Restart run`. The busy label is derived. */
  label: string
  /** What to say instead when the run is outdated. */
  outdatedNote: string
  /** Class for the note paragraph — pages style their own muted text. */
  noteClassName: string
  buttonClassName?: string
  onRestart: () => void
}

/**
 * Restart button, or the reason there isn't one.
 *
 * An outdated run cannot be restarted: it would re-test against content it
 * was never written for, and the backend refuses it too. So the control has
 * two faces, and both detail pages had them as two adjacent conditions
 * testing `isOutdated` in opposite directions — which is exactly the shape
 * that lets the two pages' wording drift apart, as it already had.
 *
 * Sits beside `OutdatedBadge`, which owns the other half of this concept.
 */
export default function RestartControl({
  run,
  enabled,
  busy,
  label,
  outdatedNote,
  noteClassName,
  buttonClassName = 'btn btn-primary',
  onRestart,
}: Props) {
  if (!enabled) return null
  if (isOutdated(run)) return <p className={noteClassName}>{outdatedNote}</p>
  return (
    <button className={buttonClassName} onClick={onRestart} disabled={busy}>
      {busy ? 'Restarting…' : label}
    </button>
  )
}
