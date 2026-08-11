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
  const { labels, targets } = useBreadcrumbOverrides()

  const crumbs = matches
    .map((match) => (match.handle as RouteHandle | undefined)?.crumbs)
    .filter((value): value is CrumbSpec[] => Boolean(value))
    .at(-1)

  if (!crumbs || crumbs.length === 0) return null

  return (
    <nav aria-label="Breadcrumb" className="breadcrumb">
      <ol className="breadcrumb-list">
        {crumbs.map((crumb, index) => {
          const isCurrent = index === crumbs.length - 1
          const label = labels[crumb.id] ?? crumb.label
          const target = targets[crumb.id] ?? (crumb.path && resolvePath(crumb.path, params))

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
      </ol>
    </nav>
  )
}
