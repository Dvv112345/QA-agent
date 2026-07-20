import { useState } from 'react'
import {
  answerRequirement,
  confirmRequirement,
  deleteRequirement,
  restartRequirement,
  updateRequirement,
} from '../services/api'
import type { RequirementResponse, RequirementStatus } from '../types'
import './RequirementCard.css'

const STATUS_LABELS: Record<RequirementStatus, string> = {
  pending: 'Queued',
  analyzing: 'Analyzing',
  needs_clarification: 'Needs clarification',
  ready: 'Ready',
  confirmed: 'Confirmed',
  failed: 'Failed',
}

interface Props {
  requirement: RequirementResponse
  sprintActive: boolean
  /** Requirement set frozen (test environment confirmed) — hides Remove. */
  locked?: boolean
  onUpdated: (requirement: RequirementResponse) => void
  onRemoved: (id: number) => void
}

export default function RequirementCard({
  requirement,
  sprintActive,
  locked = false,
  onUpdated,
  onRemoved,
}: Props) {
  const [answer, setAnswer] = useState('')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [showOriginal, setShowOriginal] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const { status } = requirement
  const inProgress = status === 'pending' || status === 'analyzing'
  const wasRewritten = requirement.description !== requirement.original_description
  const capReached = requirement.clarification_cap_reached

  const runAction = (
    promise: Promise<RequirementResponse>,
    onSuccess?: (updated: RequirementResponse) => void,
  ) => {
    setBusy(true)
    setError(null)
    promise
      .then((updated) => {
        onSuccess?.(updated)
        onUpdated(updated)
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setBusy(false))
  }

  const handleAnswer = () => {
    const trimmed = answer.trim()
    if (!trimmed) return
    runAction(answerRequirement(requirement.id, trimmed), () => setAnswer(''))
  }

  const handleConfirm = () => runAction(confirmRequirement(requirement.id))

  const handleRestart = () => runAction(restartRequirement(requirement.id))

  const startEditing = () => {
    setDraft(requirement.description)
    setEditing(true)
  }

  const handleSaveEdit = () => {
    const trimmed = draft.trim()
    if (!trimmed) return
    runAction(updateRequirement(requirement.id, trimmed), () => setEditing(false))
  }

  const handleRemove = () => {
    if (!window.confirm(`Remove requirement "${requirement.name}"?`)) return
    setBusy(true)
    setError(null)
    deleteRequirement(requirement.id)
      .then(() => onRemoved(requirement.id))
      .catch((err: Error) => {
        setError(err.message)
        setBusy(false)
      })
  }

  return (
    <article className={`requirement-card requirement-card-${status}`}>
      <header className="requirement-card-header">
        <h3>{requirement.name}</h3>
        <div className="requirement-card-badges">
          {requirement.from_prd && <span className="req-badge req-badge-prd">From PRD</span>}
          <span className={`req-badge req-badge-${status}`}>{STATUS_LABELS[status]}</span>
        </div>
      </header>

      {inProgress ? (
        <div className="requirement-card-progress">
          <span className="requirement-spinner" aria-hidden="true" />
          <p>{requirement.description}</p>
        </div>
      ) : editing ? (
        <div className="requirement-card-edit">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={4}
            aria-label="Edit description"
            disabled={busy}
          />
          <div className="requirement-card-actions">
            <button
              className="btn btn-primary"
              onClick={handleSaveEdit}
              disabled={busy || !draft.trim()}
            >
              Save
            </button>
            <button className="btn btn-secondary" onClick={() => setEditing(false)} disabled={busy}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <>
          <p className="requirement-card-description">{requirement.description}</p>

          {wasRewritten && (
            <div className="requirement-card-original">
              <button
                className="btn-link"
                onClick={() => setShowOriginal((prev) => !prev)}
                type="button"
              >
                {showOriginal ? 'Hide original' : 'Show original'}
              </button>
              {showOriginal && <p>{requirement.original_description}</p>}
            </div>
          )}

          {status === 'failed' && requirement.error && (
            <p className="requirement-card-failure">{requirement.error}</p>
          )}

          {status === 'needs_clarification' && (
            <div className="requirement-card-question">
              <p className="requirement-card-question-text">{requirement.clarifying_question}</p>
              {sprintActive &&
                (capReached ? (
                  <p className="requirement-card-cap-notice">
                    Clarification limit reached — confirm as-is or edit the description manually.
                  </p>
                ) : (
                  <div className="requirement-card-answer">
                    <textarea
                      value={answer}
                      onChange={(e) => setAnswer(e.target.value)}
                      placeholder="Your answer…"
                      aria-label="Clarification answer"
                      rows={2}
                      disabled={busy}
                    />
                    <button
                      className="btn btn-primary"
                      onClick={handleAnswer}
                      disabled={busy || !answer.trim()}
                    >
                      Submit answer
                    </button>
                  </div>
                ))}
            </div>
          )}

          {sprintActive && (
            <div className="requirement-card-actions">
              {status === 'needs_clarification' && (
                <button className="btn btn-secondary" onClick={handleConfirm} disabled={busy}>
                  Confirm as-is
                </button>
              )}
              {status === 'ready' && (
                <button className="btn btn-primary" onClick={handleConfirm} disabled={busy}>
                  Confirm
                </button>
              )}
              {(status === 'needs_clarification' || status === 'ready') && (
                <button className="btn btn-secondary" onClick={startEditing} disabled={busy}>
                  Edit
                </button>
              )}
              {status === 'failed' && (
                <button className="btn btn-primary" onClick={handleRestart} disabled={busy}>
                  Restart
                </button>
              )}
              {!locked && (
                <button className="btn btn-danger" onClick={handleRemove} disabled={busy}>
                  Remove
                </button>
              )}
            </div>
          )}
        </>
      )}

      {sprintActive && inProgress && !locked && (
        <div className="requirement-card-actions">
          <button className="btn btn-danger" onClick={handleRemove} disabled={busy}>
            Remove
          </button>
        </div>
      )}

      {error && <p className="requirement-card-error">{error}</p>}
    </article>
  )
}
