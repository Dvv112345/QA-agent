import { useEffect, useState } from 'react'
import './ScrollToTop.css'

/**
 * A button back to the top, shown once the page has scrolled far enough.
 *
 * Rendered globally in `RootLayout` rather than added to a list of "long"
 * pages — a list goes stale the moment a page grows, and on a short page this
 * simply never appears. Several pages here render unbounded lists at an 18px
 * base font, so they get very tall.
 *
 * `#root` is a flex column with `min-height: 100svh`, so the window is the
 * scroll container: `window.scrollY` and `window.scrollTo` are the right APIs
 * and no portal is needed.
 */

/** Roughly one viewport — far enough that the top is genuinely out of reach. */
const SHOW_AFTER_PX = 600

export default function ScrollToTop() {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    // Passive, and it only ever flips a boolean — never per-event state, which
    // would re-render on every scroll frame.
    const onScroll = () => setVisible(window.scrollY > SHOW_AFTER_PX)

    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  if (!visible) return null

  return (
    <button
      type="button"
      className="scroll-to-top"
      aria-label="Scroll to top"
      onClick={() => {
        const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
        window.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' })
      }}
    >
      <span aria-hidden="true">&uarr;</span>
    </button>
  )
}
