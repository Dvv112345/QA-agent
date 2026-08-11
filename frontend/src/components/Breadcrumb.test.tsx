import { render, renderHook, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { BreadcrumbProvider, useCrumb } from '../BreadcrumbContext'
import { routes } from '../router'
import Breadcrumb from './Breadcrumb'

/**
 * Mounts the breadcrumb against the app's **real** route table, so the
 * `handle.crumbs` chains in `router.tsx` and this component are exercised
 * together rather than each against a copy of the other.
 *
 * Only the `element` of each route is swapped for a stub. Everything the
 * breadcrumb actually reads — `path`, `handle`, the ancestor chains — is the
 * genuine article; rendering the real pages would just fire their fetches
 * without testing anything more.
 */

const realChildren = routes[0].children

function renderAt(path: string, ui: React.ReactNode = null) {
  const router = createMemoryRouter(
    [
      {
        element: (
          <BreadcrumbProvider>
            <Breadcrumb />
            {ui}
          </BreadcrumbProvider>
        ),
        children: realChildren.map((child) => ({ ...child, element: <div /> })),
      },
    ],
    { initialEntries: [path] },
  )
  return render(<RouterProvider router={router} />)
}

/**
 * One case per route. `publishes` names the crumb ids that route's page hands
 * to `useCrumb` — the id is the only thing joining `router.tsx` to a page's
 * enrichment call, and a typo on either side silently degrades to the generic
 * label with nothing failing.
 */
const CASES: Array<{
  pattern: string
  url: string
  chain: string[]
  publishes?: string[]
}> = [
  { pattern: '/', url: '/', chain: ['Sprints'] },
  { pattern: '/sprints/new', url: '/sprints/new', chain: ['Sprints', 'New Sprint'] },
  {
    pattern: '/sprints/:id',
    url: '/sprints/7',
    chain: ['Sprints', 'Sprint'],
    publishes: ['sprint'],
  },
  {
    pattern: '/sprints/:id/test-environment',
    url: '/sprints/7/test-environment',
    chain: ['Sprints', 'Sprint', 'Test Environment'],
    publishes: ['sprint'],
  },
  {
    pattern: '/sprints/:id/test-plans',
    url: '/sprints/7/test-plans',
    chain: ['Sprints', 'Sprint', 'Test Plans'],
    publishes: ['sprint'],
  },
  {
    pattern: '/sprints/:id/test-runs',
    url: '/sprints/7/test-runs',
    chain: ['Sprints', 'Sprint', 'Test Runs'],
    publishes: ['sprint'],
  },
  {
    pattern: '/sprints/:id/test-runs/:runId',
    url: '/sprints/7/test-runs/3',
    chain: ['Sprints', 'Sprint', 'Test Runs', 'Run'],
    publishes: ['sprint', 'run'],
  },
  {
    pattern: '/sprints/:id/exploratory-runs/:runId',
    url: '/sprints/7/exploratory-runs/3',
    chain: ['Sprints', 'Sprint', 'Test Runs', 'Exploratory Run'],
    publishes: ['run'],
  },
  {
    pattern: '/sprints/:id/exploratory-sessions/:sessionId',
    url: '/sprints/7/exploratory-sessions/9',
    chain: ['Sprints', 'Sprint', 'Test Runs', 'Exploratory Run', 'Session Sheet'],
    publishes: ['run'],
  },
  { pattern: '/repos', url: '/repos', chain: ['Sprints', 'Repositories'] },
]

describe('Breadcrumb', () => {
  it('has a case here for every route in the real table', () => {
    // Fails when a route is added without deciding what its breadcrumb says.
    expect(new Set(realChildren.map((child) => child.path))).toEqual(
      new Set(CASES.map((c) => c.pattern)),
    )
  })

  it.each(CASES)('renders the full chain at $pattern', ({ url, chain }) => {
    renderAt(url)

    const nav = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect(nav.textContent).toBe(chain.join(''))

    // Every crumb but the last navigates; the last is the page you are on.
    // `queryAll`, not `getAll` — at `/` the only crumb is the current one.
    expect(screen.queryAllByRole('link').map((l) => l.textContent)).toEqual(chain.slice(0, -1))

    const current = screen.getByText(chain[chain.length - 1])
    expect(current).toHaveAttribute('aria-current', 'page')
  })

  it.each(CASES.filter((c) => c.publishes))(
    'exposes the crumb ids $pattern publishes against',
    ({ pattern, publishes }) => {
      const route = realChildren.find((child) => child.path === pattern)
      const ids = route?.handle?.crumbs.map((crumb) => crumb.id) ?? []

      // A `useCrumb('sprint', …)` on a page whose chain has no `sprint` crumb
      // is a no-op that looks like working code.
      for (const id of publishes ?? []) expect(ids).toContain(id)
    },
  )

  it('substitutes route params into ancestor links', () => {
    renderAt('/sprints/7/test-plans')

    const links = screen.getAllByRole('link')
    expect(links[0]).toHaveAttribute('href', '/')
    expect(links[1]).toHaveAttribute('href', '/sprints/7')
  })

  it('renders nothing for a route that declares no crumbs', () => {
    const router = createMemoryRouter(
      [
        {
          element: (
            <BreadcrumbProvider>
              <Breadcrumb />
            </BreadcrumbProvider>
          ),
          children: [{ path: '/nowhere', element: <div /> }],
        },
      ],
      { initialEntries: ['/nowhere'] },
    )
    render(<RouterProvider router={router} />)

    expect(screen.queryByRole('navigation', { name: 'Breadcrumb' })).not.toBeInTheDocument()
  })

  it('lets a page replace a generic label with a specific one', () => {
    function Publisher() {
      useCrumb('sprint', 'Acme Sprint')
      return null
    }
    renderAt('/sprints/7/test-plans', <Publisher />)

    expect(screen.getByRole('link', { name: 'Acme Sprint' })).toHaveAttribute('href', '/sprints/7')
    expect(screen.queryByText('Sprint')).not.toBeInTheDocument()
  })

  it('lets a page supply a target the URL cannot express', () => {
    function Publisher() {
      useCrumb('run', 'Exploratory Run #3', '/sprints/7/exploratory-runs/3')
      return null
    }
    renderAt('/sprints/7/exploratory-sessions/9', <Publisher />)

    // The published href wins over the route pattern's fallback — this is what
    // lets a session sheet link to the parent run held in its fetched row.
    expect(screen.getByRole('link', { name: 'Exploratory Run #3' })).toHaveAttribute(
      'href',
      '/sprints/7/exploratory-runs/3',
    )
  })

  it('useCrumb is inert without a provider', () => {
    // Load-bearing: `test-utils.tsx` renders every page through a bare
    // catch-all route with no RootLayout, so no provider is mounted in any
    // page test. Throwing here would break all ten of them.
    expect(() => renderHook(() => useCrumb('sprint', 'Acme Sprint'))).not.toThrow()
  })
})
