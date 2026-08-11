import type { SprintResponse } from './types'

/**
 * The pipeline stages a sprint is walked through, after its own requirements
 * stage — which is the sprint page itself, and so has no entry here.
 *
 * One definition of each stage, because three places need the identical answer:
 * the route table's breadcrumb chain, the disabled crumbs the breadcrumb shows
 * ahead of the current page, and `StageNav`'s next-stage control. The gates and
 * the reasons used to live only at the three `StageNav` call sites, so adding a
 * second consumer would have meant a second copy of the same three sentences.
 *
 * Gates read backend-computed flags on `SprintResponse` (Convention #10) and are
 * never re-derived from the rows on screen — a requirement without a plan
 * contributes no row, so counting what is rendered reports "all approved" for a
 * sprint that is not.
 */

export type StageId = 'test-environment' | 'test-plans' | 'test-runs'

interface Stage {
  label: string
  /** Route pattern, for the breadcrumb's `:id` substitution. */
  pattern: string
  /** Concrete href. `sprintId` is `Number(id)` in every page that has one. */
  href: (sprintId: number) => string
  isOpen: (sprint: SprintResponse) => boolean
  /** Shown when the gate is shut — on the crumb and on `StageNav` alike. */
  blockedReason: string
}

export const STAGES: Record<StageId, Stage> = {
  'test-environment': {
    label: 'Test Environment',
    pattern: '/sprints/:id/test-environment',
    href: (sprintId) => `/sprints/${sprintId}/test-environment`,
    isOpen: (sprint) => sprint.requirements_complete,
    blockedReason: 'Confirm every requirement to continue.',
  },
  'test-plans': {
    label: 'Test Plans',
    pattern: '/sprints/:id/test-plans',
    href: (sprintId) => `/sprints/${sprintId}/test-plans`,
    isOpen: (sprint) => sprint.environment_confirmed,
    blockedReason: 'Confirm the test environment to continue.',
  },
  'test-runs': {
    label: 'Test Runs',
    pattern: '/sprints/:id/test-runs',
    href: (sprintId) => `/sprints/${sprintId}/test-runs`,
    isOpen: (sprint) => sprint.test_plans_complete,
    blockedReason: 'Approve every test plan to continue.',
  },
}

/** In pipeline order. */
export const STAGE_IDS: StageId[] = ['test-environment', 'test-plans', 'test-runs']

/**
 * Why `stage` cannot be opened, or `null` when it can.
 *
 * A sprint that has not loaded yet blocks everything: the breadcrumb shows a
 * forward stage as reachable only once something has said so, so the load
 * window must not read as "open".
 */
export function stageBlockedReason(
  stage: StageId,
  sprint: SprintResponse | null | undefined,
): string | null {
  if (!sprint) return STAGES[stage].blockedReason
  return STAGES[stage].isOpen(sprint) ? null : STAGES[stage].blockedReason
}
