import { isOutdated, type OutdatableRun } from '../outdated'
import type { OutdatedReason } from '../types'
import './OutdatedBadge.css'

const LABELS: Record<OutdatedReason, string> = {
  requirement: 'Requirement changed',
  test_plan: 'Test plan changed',
  test_environment: 'Environment changed',
}

interface Props {
  run: OutdatableRun
}

/**
 * Explains why a run no longer reflects the sprint, and which artifact moved.
 *
 * A run is kept rather than deleted when its inputs change, so without this
 * a stale result is indistinguishable from a current one — and the reason
 * matters for reading it: a failure against a since-rewritten requirement
 * says something different from one against a since-changed environment.
 */
export default function OutdatedBadge({ run }: Props) {
  if (!isOutdated(run)) return null

  return (
    <span className="outdated-badge" title="This run tested an earlier version of the sprint">
      {run.outdated_reasons.map((reason) => (
        <span key={reason} className={`outdated-chip outdated-chip-${reason}`}>
          {/* Deletion is one of the ways a requirement can differ, so it
              shares the reason and only changes the wording. */}
          {reason === 'requirement' && run.requirement_deleted
            ? 'Requirement deleted'
            : LABELS[reason]}
        </span>
      ))}
    </span>
  )
}
