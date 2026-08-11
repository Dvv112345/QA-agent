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
  publish: (id: string, label: string | null, href?: string) => void
}

const NO_PROVIDER: Store = {
  labels: {},
  targets: {},
  publish: () => {},
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

  const store = useMemo<Store>(() => ({ labels, targets, publish }), [labels, targets, publish])

  return <BreadcrumbContext.Provider value={store}>{children}</BreadcrumbContext.Provider>
}

export function useBreadcrumbOverrides(): { labels: Overrides; targets: Overrides } {
  const { labels, targets } = useContext(BreadcrumbContext)
  return { labels, targets }
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
