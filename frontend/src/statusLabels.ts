/**
 * How each status reads in the UI.
 *
 * These maps were duplicated across pages — `ExploratoryRunDetailPage`'s
 * two were byte-identical to `TestRunsPage`'s and `ExploratorySessionPage`'s
 * respectively — so renaming a status label meant edits in three files that
 * no single grep would find together.
 *
 * Every map is keyed by its status union rather than `string`. That is the
 * point: a missing key is a type error, and the `?? status` fallback the
 * old `Record<string, string>` maps needed (which is what let them silently
 * disagree) is gone.
 */
import type {
  CicdExportStatus,
  DomainOutcome,
  ExploratoryRunStatus,
  ExploratorySessionStatus,
  NonfunctionalChildStatus,
  NonfunctionalRunStatus,
  TestCaseExecutionStatus,
  TestExecutionStatus,
} from './types'

/** CI/CD exports — "Exporting" is the honest verb while it writes. */
export const CICD_EXPORT_STATUS_LABELS: Record<CicdExportStatus, string> = {
  pending: 'Queued',
  running: 'Exporting',
  completed: 'Exported',
  failed: 'Failed',
}

/** Scripted runs and executions. */
export const RUN_STATUS_LABELS: Record<TestExecutionStatus, string> = {
  pending: 'Queued',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
}

/** Exploratory runs — same states, but "Exploring" is the honest verb. */
export const EXPLORATORY_RUN_STATUS_LABELS: Record<ExploratoryRunStatus, string> = {
  pending: 'Queued',
  running: 'Exploring',
  completed: 'Completed',
  failed: 'Failed',
}

export const SESSION_STATUS_LABELS: Record<ExploratorySessionStatus, string> = {
  pending: 'Queued',
  running: 'Exploring',
  completed: 'Completed',
  error: 'Error',
  skipped: 'Not explored',
}

/** Nonfunctional runs — "Examining" is the honest verb. */
export const NONFUNCTIONAL_RUN_STATUS_LABELS: Record<NonfunctionalRunStatus, string> = {
  pending: 'Queued',
  running: 'Examining',
  completed: 'Completed',
  failed: 'Failed',
}

/** One examined URL, or one applied load profile. */
export const NONFUNCTIONAL_CHILD_STATUS_LABELS: Record<NonfunctionalChildStatus, string> = {
  pending: 'Queued',
  running: 'In progress',
  completed: 'Done',
  error: 'Error',
  skipped: 'Not reached',
}

/**
 * What one domain found at one URL. Four labels because there are four
 * answers: a check that could not run is not a clean one, and a domain
 * that does not apply to an endpoint was never asked.
 */
export const DOMAIN_OUTCOME_LABELS: Record<DomainOutcome, string> = {
  clean: 'No violations',
  violations: 'Violations found',
  not_applicable: 'Not applicable here',
  failed_to_run: 'Could not run',
}

export const CASE_STATUS_LABELS: Record<TestCaseExecutionStatus, string> = {
  pending: 'Queued',
  running: 'Running',
  passed: 'Passed',
  failed: 'Application bug found',
  error: 'Could not determine — script may still be broken',
  skipped: 'Not run',
}

/** Why a charter session ended. Not a status union — free-form on the row. */
export const STOP_REASON_LABELS: Record<string, string> = {
  charter_complete: 'Charter explored',
  action_cap: 'Time box exhausted',
  model_stopped: 'Ended without calling finish_session',
  context_limit: 'Ran out of context room',
  error: 'Stopped by an error',
}
