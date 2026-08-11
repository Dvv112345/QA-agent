import { useEffect, useState } from 'react'
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
import { usePolling } from '../hooks/usePolling'
import './TestPlansPage.css'

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

  // The sprint carries the flags that decide what this page offers
  // (`test_plans_missing`, `test_plans_complete`), and both move when the
  // user generates or approves — so every mutation refreshes them. Polling
  // deliberately does not: generation only moves plans between pending and
  // draft, and neither flag depends on that, so refetching the sprint every
  // 2.5s would double the poll traffic for a value that cannot have changed.
  const refreshFlags = () => fetchSprint(sprintId).then(setSprint)

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

  usePolling(() => fetchTestPlans(sprintId).then(setPlans), { enabled: shouldPoll })

  const handleGenerate = () => {
    setGenerating(true)
    setActionError(null)
    generateTestPlans(sprintId)
      .then((planData) => {
        setPlans(planData)
        return refreshFlags()
      })
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

  // No confirmation: approval is not final. It gates running, not editing —
  // an edit returns the plan to draft, and that edit warns for itself.
  const handleApproveAll = () => {
    setApprovingAll(true)
    setActionError(null)
    approveAllTestPlans(sprintId)
      .then((planData) => {
        setPlans(planData)
        return refreshFlags()
      })
      .catch((err: Error) => setActionError(err.message))
      .finally(() => setApprovingAll(false))
  }

  const handleUpdated = (updated: TestPlanResponse) => {
    setPlans((prev) => prev.map((plan) => (plan.id === updated.id ? updated : plan)))
    // Approving or editing one plan moves `test_plans_complete`, which
    // gates the Continue link below.
    refreshFlags().catch(() => {
      /* the plan itself already updated — the flag catches up on the next read */
    })
  }

  if (loading) return <p className="test-plans-message">Loading test plans&hellip;</p>
  if (loadError) return <p className="test-plans-message test-plans-error">{loadError}</p>
  if (!sprint) return <p className="test-plans-message">Sprint not found.</p>

  const active = sprint.active
  // Plans can outlive the lock state (finished sprint stays readable).
  const guarded = plans.length === 0 && (!active || !sprint.environment_confirmed)
  const draftedCount = plans.filter((plan) => !isInProgress(plan)).length
  const approvedCount = plans.filter((plan) => plan.status === 'approved').length
  // Both backend-computed (Convention #10). `allApproved` used to be derived
  // from `plans` alone, which reported "All test plans approved" for a sprint
  // whose edited requirement had no plan at all: a requirement without a plan
  // contributes no row, so there was nothing in the array to fall short.
  const allApproved = sprint.test_plans_complete
  const missingPlans = sprint.test_plans_missing

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

          {/* Editing a confirmed requirement removes the plan written against
              its old text, and nothing regenerates it. The plan list cannot
              show this — a requirement with no plan contributes no row — so
              without this block the page looked complete and the button that
              rebuilds the plan was unreachable, since it only rendered when
              there were no plans at all. */}
          {missingPlans &&
            active &&
            (sprint.environment_confirmed ? (
              <div className="test-plans-generate">
                <p className="test-plans-hint">
                  A requirement has no test plan. Editing a requirement removes the plan written
                  against its earlier text — generate a replacement to cover it again.
                </p>
                <button className="btn btn-primary" onClick={handleGenerate} disabled={generating}>
                  {generating ? 'Starting…' : 'Generate missing test plans'}
                </button>
              </div>
            ) : (
              <p className="test-plans-notice">
                A requirement changed, so its test plan was removed and the test environment needs
                re-checking before a replacement can be generated.{' '}
                <Link to={`/sprints/${sprintId}/test-environment`}>
                  Re-check the test environment
                </Link>
                , then come back here.
              </p>
            ))}

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
