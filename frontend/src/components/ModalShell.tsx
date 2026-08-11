import { useEffect, useId, useRef, type ReactNode } from 'react'
import './ModalShell.css'

/**
 * The overlay, card and keyboard contract every modal in the app shares.
 *
 * The four hand-rolled modals that preceded this each declared
 * `role="dialog" aria-modal="true"` and then implemented none of what that
 * promises — no Escape, no focus trap, no focus restore. A keyboard user could
 * tab straight out of an open dialog into the page behind it. `LoginModal` was
 * the only one handling Escape, and nothing followed it.
 *
 * Dismissal is blocked while `busy`, so a request in flight cannot be
 * abandoned halfway by a stray Escape or a click on the backdrop.
 */

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

interface Props {
  title: string
  /** Blocks Escape and backdrop dismissal while an action is in flight. */
  busy?: boolean
  /** Widen the card for content-heavy dialogs (charters, tracker config). */
  wide?: boolean
  /**
   * Extra class on the card, for a dialog that owns its own sizing. Pair it
   * with `.modal-card` in the selector — `.modal-card.login-card` — so the
   * override wins on specificity rather than on stylesheet import order.
   */
  cardClassName?: string
  /**
   * Omit for a dialog that must not be dismissed. The login gate has nothing
   * behind it to return to, so Escape and the backdrop do nothing there.
   */
  onClose?: () => void
  children: ReactNode
}

export default function ModalShell({
  title,
  busy = false,
  wide = false,
  cardClassName,
  onClose,
  children,
}: Props) {
  const cardRef = useRef<HTMLDivElement>(null)
  const titleId = useId()

  // Latest-ref, refreshed in an effect with no dependency array. Assigning
  // during render is banned by `react-hooks/refs`, and putting the callback in
  // the key handler's deps would re-register the listener every render.
  const closeRef = useRef(onClose)
  const busyRef = useRef(busy)
  useEffect(() => {
    closeRef.current = onClose
    busyRef.current = busy
  })

  // Move focus into the dialog on open and return it to the trigger on close.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const card = cardRef.current
    const first = card?.querySelector<HTMLElement>(FOCUSABLE)
    ;(first ?? card)?.focus()
    return () => previous?.focus?.()
  }, [])

  // Escape cancels; Tab cycles within the card rather than escaping behind it.
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (!busyRef.current) closeRef.current?.()
        return
      }
      if (event.key !== 'Tab') return

      const items = Array.from(cardRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])
      if (items.length === 0) return

      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [])

  return (
    // The backdrop is a click target, not a control: dismissing by clicking
    // outside is a convenience, and the Escape handler above is the accessible
    // equivalent, so it needs no role or key handler of its own.
    <div
      className="modal-overlay"
      onClick={(event) => {
        // Only a click on the backdrop itself — not one bubbling out of the card.
        if (event.target === event.currentTarget && !busy) onClose?.()
      }}
    >
      <div
        ref={cardRef}
        className={['modal-card', wide && 'modal-card-wide', cardClassName]
          .filter(Boolean)
          .join(' ')}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <h2 id={titleId}>{title}</h2>
        {children}
      </div>
    </div>
  )
}
