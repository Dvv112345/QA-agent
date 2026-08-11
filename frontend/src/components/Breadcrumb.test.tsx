import { render, renderHook, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { BreadcrumbProvider, useCrumb } from '../BreadcrumbContext'
import Breadcrumb from './Breadcrumb'

/**
 * Mounts the breadcrumb against the app's *real* route table, so the `handle`
 * chains in `router.tsx` and this component are exercised together. Importing
 * `routes` would pull in every page; the chains are restated here in the same
 * shape, and `renders every route's chain` walks the real one.
 */
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
        children: [
          {
            path: '/',
            element: <div />,
            handle: { crumbs: [{ id: 'sprints', label: 'Sprints', path: '/' }] },
          },
          {
            path: '/sprints/:id/test-plans',
            element: <div />,
            handle: {
              crumbs: [
                { id: 'sprints', label: 'Sprints', path: '/' },
                { id: 'sprint', label: 'Sprint', path: '/sprints/:id' },
                { id: 'test-plans', label: 'Test Plans' },
              ],
            },
          },
          { path: '/nowhere', element: <div /> },
        ],
      },
    ],
    { initialEntries: [path] },
  )
  return render(<RouterProvider router={router} />)
}

describe('Breadcrumb', () => {
  it('renders the ancestor chain from the route handle, with no page cooperation', () => {
    renderAt('/sprints/7/test-plans')

    const nav = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect(nav).toBeInTheDocument()

    const links = screen.getAllByRole('link')
    expect(links.map((l) => l.textContent)).toEqual(['Sprints', 'Sprint'])
    expect(links[0]).toHaveAttribute('href', '/')
    // `:id` is substituted from the current URL.
    expect(links[1]).toHaveAttribute('href', '/sprints/7')
  })

  it('marks the final crumb as the current page and does not link it', () => {
    renderAt('/sprints/7/test-plans')

    const current = screen.getByText('Test Plans')
    expect(current).toHaveAttribute('aria-current', 'page')
    expect(screen.queryByRole('link', { name: 'Test Plans' })).not.toBeInTheDocument()
  })

  it('renders nothing for a route that declares no crumbs', () => {
    renderAt('/nowhere')

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
      useCrumb('sprint', 'Acme Sprint', '/sprints/99/test-runs')
      return null
    }
    renderAt('/sprints/7/test-plans', <Publisher />)

    // The published href wins over the route pattern's substitution — this is
    // what lets a session sheet link to the parent run held in its fetched row.
    expect(screen.getByRole('link', { name: 'Acme Sprint' })).toHaveAttribute(
      'href',
      '/sprints/99/test-runs',
    )
  })

  it('useCrumb is inert without a provider', () => {
    // Load-bearing: `test-utils.tsx` renders every page through a bare
    // catch-all route with no RootLayout, so no provider is mounted in any
    // page test. Throwing here would break all ten of them.
    expect(() => renderHook(() => useCrumb('sprint', 'Acme Sprint'))).not.toThrow()
  })
})
