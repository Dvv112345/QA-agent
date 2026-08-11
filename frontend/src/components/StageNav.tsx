import { useId } from 'react'
import { Link } from 'react-router-dom'
import { STAGES, type StageId } from '../stages'
import type { SprintResponse } from '../types'
import './StageNav.css'

/**
 * The link to the next pipeline stage, at the top of the page.
 *
 * Every forward control used to sit at the *bottom*, below content that grows
 * without bound, and two of the three only appeared once their gate was
 * already satisfied — so the next step was invisible precisely while the user
 * was working toward it. This renders always, and says why when it is shut.
 *
 * Labelled by destination rather than "Continue", which reads wrong on a
 * finished sprint where the downstream pages are still readable.
 *
 * The label, URL, gate and blocking reason all come from `stages.ts`, which the
 * breadcrumb's dimmed forward crumbs read too — the two say the same thing about
 * the same stage, so they must not be able to drift.
 */

interface Props {
  /** The stage this page leads to. Everything else comes from `stages.ts`. */
  stage: StageId
  sprintId: number
  sprint: SprintResponse
}

export default function StageNav({ stage, sprintId, sprint }: Props) {
  const reasonId = useId()
  const { label, href, isOpen, blockedReason } = STAGES[stage]
  const ready = isOpen(sprint)
  const to = href(sprintId)

  return (
    <div className="stage-nav">
      {ready ? (
        <Link to={to} className="btn btn-primary">
          {label} &rarr;
        </Link>
      ) : (
        <>
          {/* A real disabled button, not a styled span. The whole point of this
              control is that its *shut* state carries information, and a span
              with `aria-disabled` announces as plain text — neither "link" nor
              "disabled". `aria-describedby` attaches the reason, so it is read
              as part of the control rather than as unrelated prose nearby. */}
          <button type="button" className="btn btn-primary" disabled aria-describedby={reasonId}>
            {label} &rarr;
          </button>
          <p className="stage-nav-reason" id={reasonId}>
            {blockedReason}
          </p>
        </>
      )}
    </div>
  )
}
