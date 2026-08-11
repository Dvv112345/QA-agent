import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import ModalShell from './ModalShell'
import { createTestRun } from '../services/api'
import type { IssueTrackerConfig, TestPlanResponse } from '../types'
import './RunTestModal.css'

interface Props {
  sprintId: number
  /**
   * The sprint's approved test plans, passed down for the same reason
   * `tracker` is: the parent already knows them, and fetching here meant
   * pulling every plan's full `cases[]` on each modal open to render a
   * list of requirement names.
   */
  plans: TestPlanResponse[]
  /** The sprint's tracker, passed down so the modal needs no second fetch. */
  tracker?: IssueTrackerConfig | null
  onClose: () => void
}

export default function RunTestModal({ sprintId, plans, tracker, onClose }: Props) {
  const navigate = useNavigate()
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Checked by default when a tracker is connected: connecting one is
  // itself the statement that findings should go there, so making the
  // user re-affirm it per run would be asking twice.
  const [exportFindings, setExportFindings] = useState(Boolean(tracker))

  const toggle = (requirementId: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(requirementId)) next.delete(requirementId)
      else next.add(requirementId)
      return next
    })
  }

  const handleStart = () => {
    if (selected.size === 0) return
    setBusy(true)
    setError(null)
    createTestRun(sprintId, Array.from(selected), exportFindings)
      .then((run) => {
        navigate(`/sprints/${sprintId}/test-runs/${run.id}`)
      })
      .catch((err: Error) => {
        setError(err.message)
        setBusy(false)
      })
  }

  return (
    <ModalShell title="Run new test" busy={busy} onClose={onClose}>
      {plans.length === 0 ? (
        <p className="run-test-message">No requirements have an approved test plan yet.</p>
      ) : (
        <ul className="run-test-list">
          {plans.map((plan) => (
            <li key={plan.requirement_id} className="run-test-item">
              <label>
                <input
                  type="checkbox"
                  checked={selected.has(plan.requirement_id)}
                  onChange={() => toggle(plan.requirement_id)}
                  disabled={busy}
                />
                {plan.requirement_name}
              </label>
            </li>
          ))}
        </ul>
      )}

      <label className="run-test-export">
        <input
          type="checkbox"
          checked={exportFindings}
          onChange={(e) => setExportFindings(e.target.checked)}
          disabled={busy || !tracker}
        />
        {tracker
          ? `File bug findings to ${tracker.target_label}`
          : 'File bug findings to an issue tracker (none connected)'}
      </label>

      {error && <p className="run-test-error">{error}</p>}

      <div className="run-test-actions">
        <button
          className="btn btn-primary"
          onClick={handleStart}
          disabled={busy || selected.size === 0}
        >
          {busy ? 'Starting…' : 'Start run'}
        </button>
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
      </div>
    </ModalShell>
  )
}
