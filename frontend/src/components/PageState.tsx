import './PageState.css'

/**
 * The centred paragraph a page shows instead of itself while loading, on
 * error, or when there is nothing to list.
 *
 * Ten pages hand-wrote this with their own class pair — `.sprint-detail-message`
 * / `.sprint-detail-error`, `.exp-run-message` / `.exp-run-error`, and so on —
 * backed by near-identical rules in ten stylesheets.
 *
 * This is consistency work rather than a navigation fix: since the breadcrumb
 * moved above the outlet, an error state no longer strands the user.
 */

interface Props {
  kind: 'loading' | 'error' | 'empty'
  children: React.ReactNode
}

export default function PageState({ kind, children }: Props) {
  return (
    <p
      className={kind === 'error' ? 'page-state page-state-error' : 'page-state'}
      role={kind === 'error' ? 'alert' : undefined}
    >
      {children}
    </p>
  )
}
