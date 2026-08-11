import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ScrollToTop from './ScrollToTop'

function scrollTo(y: number) {
  Object.defineProperty(window, 'scrollY', { value: y, writable: true, configurable: true })
  fireEvent.scroll(window)
}

afterEach(() => {
  scrollTo(0)
  vi.restoreAllMocks()
})

describe('ScrollToTop', () => {
  it('stays hidden until the page has scrolled far enough', () => {
    render(<ScrollToTop />)

    // A short page never shows it at all, which is why this can live globally
    // rather than on a hand-maintained list of "long" pages.
    expect(screen.queryByRole('button', { name: 'Scroll to top' })).not.toBeInTheDocument()

    scrollTo(700)
    expect(screen.getByRole('button', { name: 'Scroll to top' })).toBeInTheDocument()

    scrollTo(100)
    expect(screen.queryByRole('button', { name: 'Scroll to top' })).not.toBeInTheDocument()
  })

  it('scrolls to the top when clicked', () => {
    const scrollSpy = vi.fn()
    window.scrollTo = scrollSpy as unknown as typeof window.scrollTo
    render(<ScrollToTop />)
    scrollTo(700)

    fireEvent.click(screen.getByRole('button', { name: 'Scroll to top' }))

    expect(scrollSpy).toHaveBeenCalledWith({ top: 0, behavior: 'smooth' })
  })

  it('skips the animation when the user asks for reduced motion', () => {
    const scrollSpy = vi.fn()
    window.scrollTo = scrollSpy as unknown as typeof window.scrollTo
    window.matchMedia = vi.fn().mockReturnValue({ matches: true }) as unknown as typeof matchMedia
    render(<ScrollToTop />)
    scrollTo(700)

    fireEvent.click(screen.getByRole('button', { name: 'Scroll to top' }))

    expect(scrollSpy).toHaveBeenCalledWith({ top: 0, behavior: 'auto' })
  })

  it('registers a passive listener and removes it on unmount', () => {
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')

    const { unmount } = render(<ScrollToTop />)

    // Passive matters: the handler runs on every scroll frame.
    expect(add).toHaveBeenCalledWith('scroll', expect.any(Function), { passive: true })

    unmount()
    expect(remove).toHaveBeenCalledWith('scroll', expect.any(Function))
  })
})
