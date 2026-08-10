import type { ExportRollup } from './types'

/**
 * Whether a run's findings are probably still being filed.
 *
 * The backend files findings *after* the commit that marks a run
 * `completed`, so there is a window where the run reads terminal and its
 * tickets do not exist yet. A page that stops polling on "not running"
 * tears its interval down inside that window and shows the export as
 * never having happened.
 *
 * Keyed on `completed` specifically, not on any terminal status: a run
 * that `failed` does not export automatically at all, so waiting for it
 * would poll forever.
 *
 * A derived predicate with one home, like `isOutdated` in `outdated.ts`,
 * and structurally typed for the same reason — it is a fact about the
 * roll-up, not about which page is asking.
 */
export function awaitingExport(run: ExportRollup & { status: string }): boolean {
  return (
    run.status === 'completed' &&
    run.export_findings &&
    run.unexported_finding_count > 0 &&
    run.export_error_count === 0
  )
}
