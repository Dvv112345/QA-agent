import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import TestPlanCard from '../components/TestPlanCard'
import {
  approveAllTestPlans,
  fetchSprint,
  fetchTestPlans,
  finishSprint,
  generateTestPlans,
} from '../services/api'
import type { SprintResponse, TestPlanResponse } from '../types'
import './TestPlansPage.css'

const POLL_INTERVAL_MS = 2500

// Statuses that still change without user input — worth polling for.
function isInProgress(plan: TestPlanResponse): boolean {
  return plan.status === 'pending' || plan.status === 'generating'
}

export default function TestPlansPage() {
  const { id } = useParams<{ id: string }>()
  const sprintId = Number(id)

  const [sprint, setSprint] = useState<SprintResponse | null>(null)
  const [plans, setPlans] = useState<TestPlanResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [generating, setGenerating] = useState(false)
  const [finishing, setFinishing] = useState(false)
  const [approvingAll, setApprovingAll] = useState(false)

  const fetchingRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([fetchSprint(sprintId), fetchTestPlans(sprintId)])
      .then(([sprintData, planData]) => {
        if (!cancelled) {
          setSprint(sprintData)
          setPlans(planData)
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

  // Poll while any plan is still queued or being generated.
  const shouldPoll = plans.some(isInProgress)
  const approvableCount = plans.filter((plan) => plan.status === 'draft').length

  useEffect(() => {
    if (!shouldPoll) return

    const pollId = setInterval(() => {
      if (fetchingRef.current) return
      fetchingRef.current = true
      fetchTestPlans(sprintId)
        .then(setPlans)
        .catch(() => {
          /* transient poll failure — retry on next tick */
        })
        .finally(() => {
          fetchingRef.current = false
        })
    }, POLL_INTERVAL_MS)

    return () => clearInterval(pollId)
  }, [shouldPoll, sprintId])

  const handleGenerate = () => {
    setGenerating(true)
    setActionError(null)
    generateTestPlans(sprintId)
      .then(setPlans)
      .catch((err: Error) => setActionError(err.message))
      .finally(() => setGenerating(false))
  }

  const handleFinish = () => {
    setFinishing(true)
    setActionError(null)
    finishSprint(sprintId)
      .then(setSprint)
      .catch((err: Error) => setActionError(err.message))
      .finally(() => setFinishing(false))
  }

  const handleApproveAll = () => {
    if (!window.confirm(`Approve all ${approvableCount} draft test plan(s)? This is final.`)) return
    setApprovingAll(true)
    setActionError(null)
    approveAllTestPlans(sprintId)
      .then(setPlans)
      .catch((err: Error) => setActionError(err.message))
      .finally(() => setApprovingAll(false))
  }

  const handleUpdated = (updated: TestPlanResponse) => {
    setPlans((prev) => prev.map((plan) => (plan.id === updated.id ? updated : plan)))
  }

  if (loading) return <p className="test-plans-message">Loading test plans&hellip;</p>
  if (loadError) return <p className="test-plans-message test-plans-error">{loadError}</p>
  if (!sprint) return <p className="test-plans-message">Sprint not found.</p>

  const active = sprint.active
  // Plans can outlive the lock state (finished sprint stays readable).
  const guarded = plans.length === 0 && (!active || !sprint.requirements_locked)
  const draftedCount = plans.filter((plan) => !isInProgress(plan)).length
  const approvedCount = plans.filter((plan) => plan.status === 'approved').length
  const allApproved = plans.length > 0 && approvedCount === plans.length

  return (
    <div className="test-plans">
      <nav className="back-links">
        <Link to="/" className="back-link">
          &larr; Back to Sprints
        </Link>
        <Link to={`/sprints/${sprintId}/test-environment`} className="back-link">
          &larr; Back to Test Environment
        </Link>
      </nav>

      <header className="test-plans-header">
        <h1>Test Plans</h1>
      </header>

      <p className="test-plans-sprint-name">{sprint.name}</p>

      {guarded ? (
        <p className="test-plans-notice">
          {active ? 'Confirm the test environment first.' : 'This sprint is finished.'}
        </p>
      ) : plans.length === 0 ? (
        <div className="test-plans-generate">
          <p className="test-plans-hint">
            Generate a test plan for each confirmed requirement. The AI reads the repository to
            ground test cases in the real code — this can take a few minutes.
          </p>
          <button className="btn btn-primary" onClick={handleGenerate} disabled={generating}>
            {generating ? 'Starting…' : 'Generate test plans'}
          </button>
        </div>
      ) : (
        <>
          <p className="test-plans-summary">
            {draftedCount} of {plans.length} plans drafted &middot; {approvedCount} approved
          </p>

          {active && (
            <div className="test-plans-approve-all">
              <button
                className="btn btn-primary"
                onClick={handleApproveAll}
                disabled={approvingAll || shouldPoll || approvableCount === 0}
              >
                {approvingAll ? 'Approving…' : `Approve all (${approvableCount})`}
              </button>
              {shouldPoll && (
                <p className="test-plans-approve-all-hint">
                  Waiting for generation to finish&hellip;
                </p>
              )}
            </div>
          )}

          {allApproved && (
            <>
              <p className="test-plans-complete">All test plans approved.</p>
              <div className="test-plans-continue">
                <Link to={`/sprints/${sprintId}/test-runs`} className="btn btn-primary">
                  Continue to Test Runs
                </Link>
              </div>
            </>
          )}

          {plans.map((plan) => (
            <TestPlanCard
              key={plan.id}
              plan={plan}
              sprintActive={active}
              onUpdated={handleUpdated}
            />
          ))}
        </>
      )}

      {actionError && <p className="test-plans-error">{actionError}</p>}

      {active && (
        <div className="test-plans-footer">
          <button className="btn btn-danger" disabled={finishing} onClick={handleFinish}>
            {finishing ? 'Finishing…' : 'Finish Sprint'}
          </button>
        </div>
      )}
    </div>
  )
}
