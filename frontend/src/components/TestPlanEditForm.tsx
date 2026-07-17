import { useState } from 'react'
import { updateTestPlan } from '../services/api'
import type { TestCaseInput, TestCasePriority, TestPlanResponse } from '../types'
import './TestPlanEditForm.css'

interface Props {
  plan: TestPlanResponse
  onSaved: (plan: TestPlanResponse) => void
  onCancel: () => void
}

const EMPTY_CASE: TestCaseInput = {
  title: '',
  preconditions: null,
  steps: '',
  expected_result: '',
  case_type: 'functional',
  priority: 'medium',
}

export default function TestPlanEditForm({ plan, onSaved, onCancel }: Props) {
  const [complexity, setComplexity] = useState(plan.complexity ?? 'medium')
  const [summary, setSummary] = useState(plan.summary ?? '')
  const [cases, setCases] = useState<TestCaseInput[]>(
    plan.cases.map((testCase) => ({
      title: testCase.title,
      preconditions: testCase.preconditions,
      steps: testCase.steps,
      expected_result: testCase.expected_result,
      case_type: testCase.case_type,
      priority: testCase.priority,
    })),
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const updateCase = (index: number, patch: Partial<TestCaseInput>) => {
    setCases((prev) => prev.map((item, i) => (i === index ? { ...item, ...patch } : item)))
  }

  const addCase = () => setCases((prev) => [...prev, { ...EMPTY_CASE }])

  const removeCase = (index: number) => {
    setCases((prev) => prev.filter((_, i) => i !== index))
  }

  const valid =
    cases.length > 0 &&
    cases.every(
      (item) =>
        item.title.trim() &&
        item.steps.split('\n').some((step) => step.trim()) &&
        item.expected_result.trim() &&
        item.case_type.trim(),
    )

  const handleSave = () => {
    if (!valid) return
    setBusy(true)
    setError(null)
    updateTestPlan(plan.id, {
      complexity,
      summary,
      cases: cases.map((item) => ({
        ...item,
        preconditions: item.preconditions?.trim() ? item.preconditions : null,
      })),
    })
      .then(onSaved)
      .catch((err: Error) => {
        setError(err.message)
        setBusy(false)
      })
  }

  return (
    <div className="test-plan-edit">
      <div className="test-plan-edit-field">
        <label htmlFor={`plan-${plan.id}-complexity`}>Complexity</label>
        <select
          id={`plan-${plan.id}-complexity`}
          value={complexity}
          onChange={(e) => setComplexity(e.target.value)}
          disabled={busy}
        >
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </select>
      </div>

      <div className="test-plan-edit-field">
        <label htmlFor={`plan-${plan.id}-summary`}>Summary</label>
        <textarea
          id={`plan-${plan.id}-summary`}
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
          rows={2}
          disabled={busy}
        />
      </div>

      {cases.map((testCase, index) => (
        <fieldset key={index} className="test-plan-edit-case">
          <legend>Case {index + 1}</legend>

          <div className="test-plan-edit-field">
            <label htmlFor={`plan-${plan.id}-case-${index}-title`}>Title</label>
            <input
              id={`plan-${plan.id}-case-${index}-title`}
              type="text"
              value={testCase.title}
              onChange={(e) => updateCase(index, { title: e.target.value })}
              disabled={busy}
            />
          </div>

          <div className="test-plan-edit-field">
            <label htmlFor={`plan-${plan.id}-case-${index}-preconditions`}>
              Preconditions (optional)
            </label>
            <textarea
              id={`plan-${plan.id}-case-${index}-preconditions`}
              value={testCase.preconditions ?? ''}
              onChange={(e) => updateCase(index, { preconditions: e.target.value })}
              rows={2}
              disabled={busy}
            />
          </div>

          <div className="test-plan-edit-field">
            <label htmlFor={`plan-${plan.id}-case-${index}-steps`}>Steps (one per line)</label>
            <textarea
              id={`plan-${plan.id}-case-${index}-steps`}
              value={testCase.steps}
              onChange={(e) => updateCase(index, { steps: e.target.value })}
              rows={4}
              disabled={busy}
            />
          </div>

          <div className="test-plan-edit-field">
            <label htmlFor={`plan-${plan.id}-case-${index}-expected`}>Expected result</label>
            <textarea
              id={`plan-${plan.id}-case-${index}-expected`}
              value={testCase.expected_result}
              onChange={(e) => updateCase(index, { expected_result: e.target.value })}
              rows={2}
              disabled={busy}
            />
          </div>

          <div className="test-plan-edit-row">
            <div className="test-plan-edit-field">
              <label htmlFor={`plan-${plan.id}-case-${index}-type`}>Type</label>
              <input
                id={`plan-${plan.id}-case-${index}-type`}
                type="text"
                value={testCase.case_type}
                onChange={(e) => updateCase(index, { case_type: e.target.value })}
                disabled={busy}
              />
            </div>

            <div className="test-plan-edit-field">
              <label htmlFor={`plan-${plan.id}-case-${index}-priority`}>Priority</label>
              <select
                id={`plan-${plan.id}-case-${index}-priority`}
                value={testCase.priority}
                onChange={(e) =>
                  updateCase(index, { priority: e.target.value as TestCasePriority })
                }
                disabled={busy}
              >
                <option value="high">High</option>
                <option value="medium">Medium</option>
                <option value="low">Low</option>
              </select>
            </div>

            <button
              type="button"
              className="btn btn-danger btn-small test-plan-edit-remove"
              onClick={() => removeCase(index)}
              disabled={busy || cases.length === 1}
            >
              Remove case
            </button>
          </div>
        </fieldset>
      ))}

      <button type="button" className="btn btn-secondary" onClick={addCase} disabled={busy}>
        Add case
      </button>

      <div className="test-plan-edit-actions">
        <button
          type="button"
          className="btn btn-primary"
          onClick={handleSave}
          disabled={busy || !valid}
        >
          {busy ? 'Saving…' : 'Save'}
        </button>
        <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>

      {error && <p className="test-plan-edit-error">{error}</p>}
    </div>
  )
}
