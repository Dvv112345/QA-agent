import { render, renderHook, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { BreadcrumbProvider, useCrumb, useCrumbGates } from '../BreadcrumbContext'
import { routes } from '../router'
import type { SprintResponse } from '../types'
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
  /** The trail behind and including the current page. */
  chain: string[]
  /** The stages ahead of it. Empty on every page that is not a stage page. */
  ahead: string[]
  publishes?: string[]
}> = [
  { pattern: '/', url: '/', chain: ['Sprints'], ahead: [] },
  { pattern: '/sprints/new', url: '/sprints/new', chain: ['Sprints', 'New Sprint'], ahead: [] },
  {
    pattern: '/sprints/:id',
    url: '/sprints/7',
    chain: ['Sprints', 'Sprint'],
    ahead: ['Test Environment', 'Test Plans', 'Test Runs'],
    publishes: ['sprint'],
  },
  {
    pattern: '/sprints/:id/test-environment',
    url: '/sprints/7/test-environment',
    chain: ['Sprints', 'Sprint', 'Test Environment'],
    ahead: ['Test Plans', 'Test Runs'],
    publishes: ['sprint'],
  },
  {
    pattern: '/sprints/:id/test-plans',
    url: '/sprints/7/test-plans',
    chain: ['Sprints', 'Sprint', 'Test Environment', 'Test Plans'],
    ahead: ['Test Runs'],
    publishes: ['sprint'],
  },
  {
    // The terminal stage: a full trail, nothing ahead.
    pattern: '/sprints/:id/test-runs',
    url: '/sprints/7/test-runs',
    chain: ['Sprints', 'Sprint', 'Test Environment', 'Test Plans', 'Test Runs'],
    ahead: [],
    publishes: ['sprint'],
  },
  {
    pattern: '/sprints/:id/test-runs/:runId',
    url: '/sprints/7/test-runs/3',
    chain: ['Sprints', 'Sprint', 'Test Environment', 'Test Plans', 'Test Runs', 'Run'],
    ahead: [],
    publishes: ['sprint', 'run'],
  },
  {
    pattern: '/sprints/:id/exploratory-runs/:runId',
    url: '/sprints/7/exploratory-runs/3',
    chain: ['Sprints', 'Sprint', 'Test Environment', 'Test Plans', 'Test Runs', 'Exploratory Run'],
    ahead: [],
    publishes: ['run'],
  },
  {
    pattern: '/sprints/:id/exploratory-sessions/:sessionId',
    url: '/sprints/7/exploratory-sessions/9',
    chain: [
      'Sprints',
      'Sprint',
      'Test Environment',
      'Test Plans',
      'Test Runs',
      'Exploratory Run',
      'Session Sheet',
    ],
    ahead: [],
    publishes: ['run'],
  },
  {
    // A side door off the last stage, not a stage of its own: it carries the
    // full trail but publishes no forward crumbs, because nothing follows it
    // and a sprint is not incomplete without it.
    pattern: '/sprints/:id/cicd',
    url: '/sprints/7/cicd',
    chain: ['Sprints', 'Sprint', 'Test Environment', 'Test Plans', 'Test Runs', 'CI/CD'],
    ahead: [],
    publishes: ['sprint'],
  },
  { pattern: '/repos', url: '/repos', chain: ['Sprints', 'Repositories'], ahead: [] },
]

describe('Breadcrumb', () => {
  it('has a case here for every route in the real table', () => {
    // Fails when a route is added without deciding what its breadcrumb says.
    expect(new Set(realChildren.map((child) => child.path))).toEqual(
      new Set(CASES.map((c) => c.pattern)),
    )
  })

  it.each(CASES)('renders the full sequence at $pattern', ({ url, chain, ahead }) => {
    renderAt(url)

    const nav = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect(nav.textContent).toBe([...chain, ...ahead].join(''))

    // Every crumb but the current one navigates. The forward stages are links
    // here because nothing has published a gate — a page blocks them itself.
    // `queryAll`, not `getAll`: at `/` the only crumb is the current one.
    expect(screen.queryAllByRole('link').map((l) => l.textContent)).toEqual([
      ...chain.slice(0, -1),
      ...ahead,
    ])

    // `aria-current` marks the end of the trail, not the end of the list.
    const current = screen.getByText(chain[chain.length - 1])
    expect(current).toHaveAttribute('aria-current', 'page')
  })

  it('declares forward stages on the stage pages only', () => {
    // Decided: a run detail page is inside a stage, not at one, so the pipeline
    // ahead of it would read as siblings of the run. Test Runs is terminal.
    const withForward = realChildren
      .filter((child) => (child.handle as { forward?: unknown[] }).forward?.length)
      .map((child) => child.path)

    expect(withForward).toEqual([
      '/sprints/:id',
      '/sprints/:id/test-environment',
      '/sprints/:id/test-plans',
    ])
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

  describe('forward stage gating', () => {
    function Gates({ sprint }: { sprint: SprintResponse | null }) {
      useCrumbGates(sprint)
      return null
    }

    function sprintWith(flags: Partial<SprintResponse>): SprintResponse {
      return {
        requirements_complete: false,
        environment_confirmed: false,
        test_plans_complete: false,
        ...flags,
      } as SprintResponse
    }

    it('dims a stage whose gate is shut and says why', () => {
      renderAt('/sprints/7/test-environment', <Gates sprint={sprintWith({})} />)

      expect(screen.queryByRole('link', { name: 'Test Plans' })).not.toBeInTheDocument()

      const crumb = screen.getByText('Test Plans')
      expect(crumb).toHaveAttribute('aria-disabled', 'true')
      // The reason has to be readable, not only hoverable.
      expect(crumb).toHaveAccessibleName(
        'Test Plans, unavailable: Confirm the test environment to continue.',
      )
    })

    it('turns the crumb back into a link once the gate opens', () => {
      renderAt(
        '/sprints/7/test-environment',
        <Gates sprint={sprintWith({ environment_confirmed: true })} />,
      )

      expect(screen.getByRole('link', { name: 'Test Plans' })).toHaveAttribute(
        'href',
        '/sprints/7/test-plans',
      )
      // Its own gate is open; the one after it is not.
      expect(screen.getByText('Test Runs')).toHaveAttribute('aria-disabled', 'true')
    })

    it('blocks every stage while the sprint is still loading', () => {
      // Disabled until proven open: the load window must not offer a click into
      // a page that will only tell the user to go back.
      renderAt('/sprints/7', <Gates sprint={null} />)

      for (const label of ['Test Environment', 'Test Plans', 'Test Runs']) {
        expect(screen.getByText(label)).toHaveAttribute('aria-disabled', 'true')
      }
    })

    it('leaves ancestors clickable when a forward gate is shut', () => {
      renderAt('/sprints/7/test-plans', <Gates sprint={sprintWith({})} />)

      // Test Environment is behind the user — going back to it is how the shut
      // gate ahead gets opened, so blocking applies to the forward segment only.
      expect(screen.getByRole('link', { name: 'Test Environment' })).toHaveAttribute(
        'href',
        '/sprints/7/test-environment',
      )
      expect(screen.getByText('Test Runs')).toHaveAttribute('aria-disabled', 'true')
    })
  })

  it('useCrumb is inert without a provider', () => {
    // Load-bearing: `test-utils.tsx` renders every page through a bare
    // catch-all route with no RootLayout, so no provider is mounted in any
    // page test. Throwing here would break all ten of them.
    expect(() => renderHook(() => useCrumb('sprint', 'Acme Sprint'))).not.toThrow()
  })
})
