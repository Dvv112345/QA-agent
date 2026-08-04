import type { OutdatedReason } from './types'

/**
 * The shape both run types share for staleness. Declared structurally rather
 * than against a concrete response type so scripted and exploratory runs —
 * and their list/detail variants — can all pass straight through.
 */
export interface OutdatableRun {
  outdated_reasons: OutdatedReason[]
  requirement_deleted: boolean
}

/**
 * Whether a run still describes the current state of the sprint.
 *
 * The backend deliberately does not ship an `outdated` boolean alongside the
 * reasons — two fields that must agree when one is derivable from the other.
 * This is the one place that derivation lives, so the call sites can't drift.
 *
 * Lives outside `OutdatedBadge.tsx` because a module exporting both a
 * component and a helper breaks React fast refresh.
 */
export function isOutdated(run: OutdatableRun): boolean {
  return run.outdated_reasons.length > 0
}
