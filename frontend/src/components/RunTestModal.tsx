import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { createTestRun, fetchTestPlans } from '../services/api'
import type { TestPlanResponse } from '../types'
import './RunTestModal.css'

interface Props {
  sprintId: number
  onClose: () => void
}

export default function RunTestModal({ sprintId, onClose }: Props) {
  const navigate = useNavigate()
  const [plans, setPlans] = useState<TestPlanResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchTestPlans(sprintId)
      .then((data) => {
        if (!cancelled) {
          setPlans(data.filter((plan) => plan.status === 'approved'))
          setLoading(false)
        }
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setLoadError(err.message)
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [sprintId])

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
    createTestRun(sprintId, Array.from(selected))
      .then((run) => {
        navigate(`/sprints/${sprintId}/test-runs/${run.id}`)
      })
      .catch((err: Error) => {
        setError(err.message)
        setBusy(false)
      })
  }

  return (
    <div className="run-test-overlay" role="dialog" aria-modal="true">
      <div className="run-test-card">
        <h2>Run new test</h2>

        {loading ? (
          <p className="run-test-message">Loading requirements&hellip;</p>
        ) : loadError ? (
          <p className="run-test-message run-test-error">{loadError}</p>
        ) : plans.length === 0 ? (
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
      </div>
    </div>
  )
}
