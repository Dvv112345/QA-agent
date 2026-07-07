import { type RenderOptions, render } from '@testing-library/react'
import { type ReactElement } from 'react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'

interface RenderWithRouterOptions extends RenderOptions {
  initialEntries?: Array<{ pathname: string; state?: unknown }> | string[]
}

function renderWithRouter(ui: ReactElement, options?: RenderWithRouterOptions) {
  const { initialEntries = ['/'], ...renderOptions } = options ?? {}

  const router = createMemoryRouter([{ path: '*', element: ui }], { initialEntries })

  return render(<RouterProvider router={router} />, renderOptions)
}

export { renderWithRouter }
