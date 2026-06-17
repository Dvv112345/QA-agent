import { type RenderOptions, render } from '@testing-library/react'
import { type ReactElement } from 'react'
import { MemoryRouter, type MemoryRouterProps } from 'react-router-dom'

interface RenderWithRouterOptions extends RenderOptions {
  initialEntries?: MemoryRouterProps['initialEntries']
}

function renderWithRouter(ui: ReactElement, options?: RenderWithRouterOptions) {
  const { initialEntries, ...renderOptions } = options ?? {}

  function Wrapper({ children }: { children: React.ReactNode }) {
    return <MemoryRouter initialEntries={initialEntries}>{children}</MemoryRouter>
  }

  return render(ui, { wrapper: Wrapper, ...renderOptions })
}

export { renderWithRouter }
