import { useState } from 'react'
import { approveTestPlan, restartTestPlan, submitTestPlanFeedback } from '../services/api'
import type { TestPlanResponse, TestPlanStatus } from '../types'
import TestPlanEditForm from './TestPlanEditForm'
import { useAction } from '../hooks/useAction'
import './TestPlanCard.css'

const STATUS_LABELS: Record<TestPlanStatus, string> = {
  pending: 'Queued',
  generating: 'Generating',
  draft: 'Draft',
  approved: 'Approved',
  failed: 'Failed',
}

interface Props {
  plan: TestPlanResponse
  sprintActive: boolean
  onUpdated: (plan: TestPlanResponse) => void
}

export default function TestPlanCard({ plan, sprintActive, onUpdated }: Props) {
  const [feedback, setFeedback] = useState('')
  const [editing, setEditing] = useState(false)
  const [showDescription, setShowDescription] = useState(false)

  const { status } = plan
  const inProgress = status === 'pending' || status === 'generating'

  const { busy, error, run: runAction } = useAction<TestPlanResponse>(onUpdated)

  const handleFeedback = () => {
    const trimmed = feedback.trim()
    if (!trimmed) return
    runAction(submitTestPlanFeedback(plan.id, trimmed), () => setFeedback(''))
  }

  // No confirmation: approval gates running, not editing. Editing an approved
  // plan returns it to draft, and that path keeps its own warning.
  const handleApprove = () => runAction(approveTestPlan(plan.id))

  const handleRestart = () => runAction(restartTestPlan(plan.id))

  const handleStartEditing = () => {
    // Editing an approved plan un-approves it and strands any run that used
    // the old cases, so name both before the form opens.
    if (
      status === 'approved' &&
      !window.confirm(
        `Editing the approved plan for "${plan.requirement_name}" will return it to draft ` +
          'and require re-approval. Existing test runs are kept, but marked as out of date. ' +
          'Continue?',
      )
    ) {
      return
    }
    setEditing(true)
  }

  return (
    <article className={`test-plan-card test-plan-card-${status}`}>
      <header className="test-plan-card-header">
        <h3>{plan.requirement_name}</h3>
        <div className="test-plan-card-badges">
          {plan.complexity && (
            <span className={`plan-badge plan-badge-complexity-${plan.complexity}`}>
              {plan.complexity} complexity
            </span>
          )}
          <span className={`plan-badge plan-badge-${status}`}>{STATUS_LABELS[status]}</span>
        </div>
      </header>

      {plan.requirement_description && (
        <div className="test-plan-card-requirement">
          <button
            className="btn-link"
            onClick={() => setShowDescription((prev) => !prev)}
            type="button"
          >
            {showDescription ? 'Hide requirement' : 'Show requirement'}
          </button>
          {showDescription && <p>{plan.requirement_description}</p>}
        </div>
      )}

      {inProgress ? (
        <div className="test-plan-card-progress">
          <span className="test-plan-spinner" aria-hidden="true" />
          <p>{status === 'pending' ? 'Waiting to generate…' : 'Generating test plan…'}</p>
        </div>
      ) : status === 'failed' ? (
        <>
          {plan.error && <p className="test-plan-card-failure">{plan.error}</p>}
          {sprintActive && (
            <div className="test-plan-card-actions">
              <button className="btn btn-primary" onClick={handleRestart} disabled={busy}>
                Restart
              </button>
            </div>
          )}
        </>
      ) : editing ? (
        <TestPlanEditForm
          plan={plan}
          onSaved={(updated) => {
            setEditing(false)
            onUpdated(updated)
          }}
          onCancel={() => setEditing(false)}
        />
      ) : (
        <>
          {plan.summary && <p className="test-plan-card-summary">{plan.summary}</p>}

          <ol className="test-plan-case-list">
            {plan.cases.map((testCase) => (
              <li key={testCase.id} className="test-plan-case">
                <div className="test-plan-case-header">
                  <h4>{testCase.title}</h4>
                  <div className="test-plan-case-chips">
                    <span className="case-chip">{testCase.case_type} test</span>
                    <span className={`case-chip case-chip-priority-${testCase.priority}`}>
                      {testCase.priority} priority
                    </span>
                  </div>
                </div>
                {testCase.preconditions && (
                  <p className="test-plan-case-preconditions">
                    <strong>Preconditions:</strong> {testCase.preconditions}
                  </p>
                )}
                <ol className="test-plan-case-steps">
                  {testCase.steps
                    .split('\n')
                    .filter((step) => step.trim())
                    .map((step, index) => (
                      <li key={index}>{step}</li>
                    ))}
                </ol>
                <p className="test-plan-case-expected">
                  <strong>Expected:</strong> {testCase.expected_result}
                </p>
              </li>
            ))}
          </ol>

          {sprintActive && (status === 'draft' || status === 'approved') && (
            <>
              <div className="test-plan-card-feedback">
                {plan.feedback_cap_reached ? (
                  <p className="test-plan-card-cap-notice">
                    Feedback limit reached — edit the plan directly.
                  </p>
                ) : (
                  <>
                    <textarea
                      value={feedback}
                      onChange={(e) => setFeedback(e.target.value)}
                      placeholder="Feedback for the AI, e.g. add negative cases for lockout…"
                      aria-label="Test plan feedback"
                      rows={2}
                      disabled={busy}
                    />
                    <button
                      className="btn btn-secondary"
                      onClick={handleFeedback}
                      disabled={busy || !feedback.trim()}
                    >
                      Send feedback
                    </button>
                  </>
                )}
              </div>
              <div className="test-plan-card-actions">
                {status === 'draft' && (
                  <button className="btn btn-primary" onClick={handleApprove} disabled={busy}>
                    Approve
                  </button>
                )}
                <button className="btn btn-secondary" onClick={handleStartEditing} disabled={busy}>
                  Edit
                </button>
              </div>
            </>
          )}
        </>
      )}

      {error && <p className="test-plan-card-error">{error}</p>}
    </article>
  )
}
