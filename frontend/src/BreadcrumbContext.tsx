/* eslint-disable react-refresh/only-export-components */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { stageBlockedReason } from './stages'
import type { SprintResponse } from './types'

/**
 * Lets a page improve a breadcrumb once its data lands — `sprint.name` for the
 * sprint crumb, `Run #14` for a run, and for an exploratory session sheet the
 * parent run's link, which the URL cannot supply because the session's route
 * carries only its own id.
 *
 * The route table supplies the structure; this supplies better wording and, for
 * that one case, a target. A page that never publishes anything still gets a
 * correct, navigable breadcrumb — enrichment is an improvement, never a
 * requirement.
 *
 * **The no-provider case is load-bearing.** `test/test-utils.tsx` renders every
 * page through a bare catch-all route with no `RootLayout`, so no provider is
 * mounted in any page test. `useCrumb` therefore no-ops instead of throwing the
 * customary "must be used within a Provider"; throwing would break all ten page
 * test suites the moment a page published a crumb.
 */

type Overrides = Record<string, string>

interface Store {
  labels: Overrides
  targets: Overrides
  /** Crumb id → why that stage cannot be opened yet. Absent means reachable. */
  blocked: Overrides
  publish: (id: string, label: string | null, href?: string) => void
  publishBlocked: (id: string, reason: string | null) => void
}

const NO_PROVIDER: Store = {
  labels: {},
  targets: {},
  blocked: {},
  publish: () => {},
  publishBlocked: () => {},
}

const BreadcrumbContext = createContext<Store>(NO_PROVIDER)

function withEntry(prev: Overrides, id: string, value: string | undefined): Overrides {
  if (value === undefined) {
    if (!(id in prev)) return prev
    const next = { ...prev }
    delete next[id]
    return next
  }
  // Bail when nothing moved, so a page re-publishing the same string on every
  // poll tick does not re-render the tree.
  if (prev[id] === value) return prev
  return { ...prev, [id]: value }
}

export function BreadcrumbProvider({ children }: { children: ReactNode }) {
  const [labels, setLabels] = useState<Overrides>({})
  const [targets, setTargets] = useState<Overrides>({})
  const [blocked, setBlocked] = useState<Overrides>({})

  /*
   * `publish` must be stable, and it only ever uses the updater form, so it
   * has no dependencies.
   *
   * Recreating it whenever `labels` moved caused an infinite loop: `useCrumb`
   * lists `publish` in its deps, so a new identity re-ran the effect; the
   * re-run fired the previous cleanup, which cleared the label; clearing moved
   * `labels`, which recreated `publish` again. Publishing anything at all spun
   * forever.
   */
  const publish = useCallback<Store['publish']>((id, label, href) => {
    setLabels((prev) => withEntry(prev, id, label ?? undefined))
    setTargets((prev) => withEntry(prev, id, label === null ? undefined : href))
  }, [])

  // Stable for the same reason `publish` is, and with the same consequence if
  // it stops being: a new identity re-runs the effect, whose cleanup clears the
  // entry, which moves state and recreates the identity again.
  const publishBlocked = useCallback<Store['publishBlocked']>((id, reason) => {
    setBlocked((prev) => withEntry(prev, id, reason ?? undefined))
  }, [])

  const store = useMemo<Store>(
    () => ({ labels, targets, blocked, publish, publishBlocked }),
    [labels, targets, blocked, publish, publishBlocked],
  )

  return <BreadcrumbContext.Provider value={store}>{children}</BreadcrumbContext.Provider>
}

export function useBreadcrumbOverrides(): {
  labels: Overrides
  targets: Overrides
  blocked: Overrides
} {
  const { labels, targets, blocked } = useContext(BreadcrumbContext)
  return { labels, targets, blocked }
}

/**
 * Publish a better label — and optionally a link target — for the crumb `id`.
 * Pass `null` while the data is still loading to leave the route's default.
 *
 * Both values must be primitive strings. Publishing an object would be a fresh
 * reference every render and re-run this effect forever, the same trap
 * documented for `location.state` in `useEffect` deps.
 */
export function useCrumb(id: string, label: string | null | undefined, href?: string): void {
  const { publish } = useContext(BreadcrumbContext)

  useEffect(() => {
    if (label === null || label === undefined) return
    publish(id, label, href)
    // Clear on unmount, so a name cannot outlive the page that set it.
    return () => publish(id, null)
  }, [id, label, href, publish])
}

/**
 * Publish why the stage crumb `id` cannot be opened, or `null` when it can.
 *
 * Unlike `useCrumb`, `null` is meaningful rather than "no opinion": it actively
 * clears the block, which is how a gate opening turns a dimmed crumb back into
 * a link without a remount.
 */
export function useCrumbGate(id: string, reason: string | null): void {
  const { publishBlocked } = useContext(BreadcrumbContext)

  useEffect(() => {
    publishBlocked(id, reason)
    // A block must not outlive the page that knew about it.
    return () => publishBlocked(id, null)
  }, [id, reason, publishBlocked])
}

/**
 * Publish the gate state of every pipeline stage from a page that holds the
 * sprint. A missing sprint blocks all three, so the load window reads as "not
 * yet reachable" rather than offering a click into a guarded page.
 *
 * The three calls are deliberately unconditional and separate. Deriving one map
 * and passing it as an effect dependency would be a fresh object every render
 * and re-run forever — the trap CLAUDE.md documents for `location.state`. Three
 * fixed hooks, each with a primitive string dependency, cannot do that.
 */
export function useCrumbGates(sprint: SprintResponse | null | undefined): void {
  useCrumbGate('test-environment', stageBlockedReason('test-environment', sprint))
  useCrumbGate('test-plans', stageBlockedReason('test-plans', sprint))
  useCrumbGate('test-runs', stageBlockedReason('test-runs', sprint))
}
