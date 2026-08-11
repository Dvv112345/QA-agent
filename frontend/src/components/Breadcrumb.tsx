import { Link, useMatches, useParams } from 'react-router-dom'
import { useBreadcrumbOverrides } from '../BreadcrumbContext'
import './Breadcrumb.css'

/**
 * The ancestor path of the current page, rendered in `RootLayout` above the
 * `<Outlet />`.
 *
 * Living at the layout level rather than inside each page is the point: every
 * page early-returns a bare "Loading…" or "not found" paragraph before it
 * renders anything of its own, so a breadcrumb placed inside one would vanish
 * exactly when the user is stuck. `TestRunDetailPage` used to render "Test run
 * not found." with no navigation at all.
 *
 * The routes are flat rather than nested, so `useMatches()` cannot walk to an
 * ancestor: each route declares its own chain in `handle.crumbs`.
 */

export interface CrumbSpec {
  /** Stable key a page publishes against to improve this crumb. */
  id: string
  /** Shown until a page publishes something better. */
  label: string
  /** Route pattern, `:param` substituted from the current URL. Omit for the
      current page, which is rendered as text rather than a link. */
  path?: string
}

interface RouteHandle {
  crumbs?: CrumbSpec[]
  /**
   * The pipeline stages *after* this page, shown so the bar reads as the whole
   * sequence rather than a dead end. Each renders dimmed until its gate opens.
   * Absent on the run detail pages, which sit inside a stage rather than at one.
   */
  forward?: CrumbSpec[]
}

function resolvePath(
  pattern: string,
  params: Readonly<Record<string, string | undefined>>,
): string {
  return pattern.replace(/:(\w+)/g, (whole, name: string) => params[name] ?? whole)
}

export default function Breadcrumb() {
  const matches = useMatches()
  const params = useParams()
  const { labels, targets, blocked } = useBreadcrumbOverrides()

  const handle = matches
    .map((match) => match.handle as RouteHandle | undefined)
    .filter((value): value is RouteHandle => Boolean(value?.crumbs?.length))
    .at(-1)

  if (!handle?.crumbs) return null
  const { crumbs, forward = [] } = handle

  const labelFor = (crumb: CrumbSpec) => labels[crumb.id] ?? crumb.label
  const targetFor = (crumb: CrumbSpec) =>
    targets[crumb.id] ?? (crumb.path && resolvePath(crumb.path, params))

  return (
    <nav aria-label="Breadcrumb" className="breadcrumb">
      <ol className="breadcrumb-list">
        {crumbs.map((crumb, index) => {
          // The trail behind the page. `aria-current` marks the last of *these*
          // rather than the last item in the list, since the forward stages
          // below are rendered after it.
          const isCurrent = index === crumbs.length - 1
          const label = labelFor(crumb)
          const target = targetFor(crumb)

          return (
            <li key={crumb.id} className="breadcrumb-item">
              {isCurrent || !target ? (
                <span
                  className="breadcrumb-current"
                  aria-current={isCurrent ? 'page' : undefined}
                  title={label}
                >
                  {label}
                </span>
              ) : (
                <Link to={target} className="breadcrumb-link" title={label}>
                  {label}
                </Link>
              )}
            </li>
          )
        })}

        {/* The stages still ahead. A gate only ever shuts one of these — a stage
            behind you stays clickable even if its gate has since closed, because
            you have already been there and going back is how you reopen it. */}
        {forward.map((crumb) => {
          const label = labelFor(crumb)
          const target = targetFor(crumb)
          const reason = blocked[crumb.id]

          return (
            <li key={crumb.id} className="breadcrumb-item">
              {reason || !target ? (
                <span
                  className="breadcrumb-blocked"
                  aria-disabled="true"
                  // The reason rides in the accessible name as well as the
                  // tooltip: `aria-disabled` on a span is announced unevenly,
                  // and a dimmed word with no explanation is the defect here.
                  aria-label={reason ? `${label}, unavailable: ${reason}` : undefined}
                  title={reason || label}
                >
                  {label}
                </span>
              ) : (
                <Link to={target} className="breadcrumb-link" title={label}>
                  {label}
                </Link>
              )}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}
