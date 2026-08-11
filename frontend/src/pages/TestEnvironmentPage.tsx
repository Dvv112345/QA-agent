import { useEffect, useState } from 'react'
import PageState from '../components/PageState'
import { Link, useParams } from 'react-router-dom'
import FinishSprintControl from '../components/FinishSprintControl'
import StageNav from '../components/StageNav'
import EnvVarsEditor from '../components/EnvVarsEditor'
import {
  answerTestEnvironment,
  confirmTestEnvironment,
  fetchSprint,
  fetchTestEnvironment,
  submitTestEnvironment,
} from '../services/api'
import { useCrumb, useCrumbGates } from '../BreadcrumbContext'
import type { SprintResponse, TestEnvironmentResponse, TestEnvironmentStatus } from '../types'
import './TestEnvironmentPage.css'

const STATUS_LABELS: Record<TestEnvironmentStatus, string> = {
  needs_info: 'Needs info',
  ready: 'Ready',
  confirmed: 'Confirmed',
}

export default function TestEnvironmentPage() {
  const { id } = useParams<{ id: string }>()
  const sprintId = Number(id)

  const [sprint, setSprint] = useState<SprintResponse | null>(null)
  const [testEnv, setTestEnv] = useState<TestEnvironmentResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [answer, setAnswer] = useState('')
  const [showOriginal, setShowOriginal] = useState(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([fetchSprint(sprintId), fetchTestEnvironment(sprintId)])
      .then(([sprintData, envData]) => {
        if (!cancelled) {
          setSprint(sprintData)
          setTestEnv(envData)
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

  /*
   * `environment_confirmed` gates the control at the top of this page, and all
   * three actions below can move it — in both directions. Confirming opens the
   * gate; resubmitting or answering against an already-confirmed description
   * sends the environment back for re-checking, which also deletes every test
   * plan in the sprint. Without this re-read the page would keep offering an
   * enabled "Test Plans →" pointing at a stage that had just been reopened and
   * emptied.
   */
  const refreshSprint = () =>
    fetchSprint(sprintId)
      .then(setSprint)
      .catch(() => {
        /* the environment row already updated — the flag catches up on the next read */
      })

  // The check is synchronous and can take up to a minute; keep the typed
  // text in state so a failure never loses the user's draft.
  const handleSubmit = (content: string) => {
    const trimmed = content.trim()
    if (!trimmed) return
    setBusy(true)
    setActionError(null)
    submitTestEnvironment(sprintId, trimmed)
      .then((updated) => {
        setTestEnv(updated)
        setEditing(false)
        setDraft('')
        return refreshSprint()
      })
      .catch((err: Error) => setActionError(err.message))
      .finally(() => setBusy(false))
  }

  const handleAnswer = () => {
    if (!testEnv) return
    const trimmed = answer.trim()
    if (!trimmed) return
    setBusy(true)
    setActionError(null)
    answerTestEnvironment(testEnv.id, trimmed)
      .then((updated) => {
        setTestEnv(updated)
        setAnswer('')
        return refreshSprint()
      })
      .catch((err: Error) => setActionError(err.message))
      .finally(() => setBusy(false))
  }

  const handleConfirm = () => {
    if (!testEnv) return
    setBusy(true)
    setActionError(null)
    confirmTestEnvironment(testEnv.id)
      .then((updated) => {
        setTestEnv(updated)
        return refreshSprint()
      })
      .catch((err: Error) => setActionError(err.message))
      .finally(() => setBusy(false))
  }

  useCrumb('sprint', sprint?.name)
  useCrumbGates(sprint)

  const startEditing = () => {
    if (!testEnv) return
    // Resubmitting a confirmed description removes every plan in the sprint,
    // so say so before the textarea opens. Frontend-only: the API stays
    // permissive, and this app is its only client.
    if (
      testEnv.status === 'confirmed' &&
      !window.confirm(
        'Changing the confirmed access description will delete every test plan in this ' +
          'sprint and require re-confirming. Existing test runs are kept, but marked as ' +
          'out of date. Continue?',
      )
    ) {
      return
    }
    setDraft(testEnv.content)
    setEditing(true)
  }

  if (loading) return <PageState kind="loading">Loading test environment&hellip;</PageState>
  if (loadError) return <PageState kind="error">{loadError}</PageState>
  if (!sprint) return <PageState kind="empty">Sprint not found.</PageState>

  const active = sprint.active
  const guarded = !testEnv && (!active || !sprint.requirements_complete)
  // Only a finished sprint makes this read-only now. Confirmation used to
  // as well, but a confirmed environment is editable — resubmitting re-runs
  // the check and removes the sprint's plans.
  const readOnly = testEnv !== null && !active
  const confirmed = testEnv?.status === 'confirmed'
  const wasRewritten = testEnv !== null && testEnv.content !== testEnv.original_content

  return (
    <div className="test-env">
      <FinishSprintControl sprint={sprint} onFinished={setSprint} />

      <nav className="page-nav">
        <Link
          to={`/sprints/${sprintId}`}
          className="btn btn-secondary"
          aria-label="Back to Requirements"
        >
          &larr; Back
        </Link>
        <StageNav stage="test-plans" sprintId={sprintId} sprint={sprint} />
      </nav>

      <header className="test-env-header">
        <h1>Test Environment Access</h1>
        {testEnv && (
          <span className={`env-badge env-badge-${testEnv.status}`}>
            {STATUS_LABELS[testEnv.status]}
          </span>
        )}
      </header>

      <p className="test-env-sprint-name">{sprint.name}</p>

      {guarded ? (
        <p className="test-env-notice">
          {active ? 'Confirm all requirements first.' : 'This sprint is finished.'}
        </p>
      ) : !testEnv || editing ? (
        <div className="test-env-form">
          <p className="test-env-hint">
            Describe how to access the test environment: how to reach each service under test and
            what credentials to use (or how to obtain them).
          </p>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={8}
            aria-label="Test environment access description"
            placeholder="e.g. The staging frontend runs at https://staging.example.com — log in as qa@example.com with the password from the team vault…"
            disabled={busy}
          />
          <div className="test-env-actions">
            <button
              className="btn btn-primary"
              onClick={() => handleSubmit(draft)}
              disabled={busy || !draft.trim()}
            >
              {busy ? 'Checking…' : editing ? 'Resubmit' : 'Submit'}
            </button>
            {editing && (
              <button
                className="btn btn-secondary"
                onClick={() => setEditing(false)}
                disabled={busy}
              >
                Cancel
              </button>
            )}
          </div>
        </div>
      ) : (
        <>
          <p className="test-env-content">{testEnv.content}</p>

          {wasRewritten && (
            <div className="test-env-original">
              <button
                className="btn-link"
                onClick={() => setShowOriginal((prev) => !prev)}
                type="button"
              >
                {showOriginal ? 'Hide original' : 'Show original'}
              </button>
              {showOriginal && <p>{testEnv.original_content}</p>}
            </div>
          )}

          {testEnv.status === 'needs_info' && (
            <div className="test-env-question">
              <p className="test-env-question-text">{testEnv.clarifying_question}</p>
              {!readOnly &&
                (testEnv.clarification_cap_reached ? (
                  <p className="test-env-cap-notice">
                    Clarification limit reached — edit the text directly to continue.
                  </p>
                ) : (
                  <div className="test-env-answer">
                    <textarea
                      value={answer}
                      onChange={(e) => setAnswer(e.target.value)}
                      placeholder="Your answer…"
                      aria-label="Clarification answer"
                      rows={3}
                      disabled={busy}
                    />
                    <button
                      className="btn btn-primary"
                      onClick={handleAnswer}
                      disabled={busy || !answer.trim()}
                    >
                      {busy ? 'Checking…' : 'Submit answer'}
                    </button>
                  </div>
                ))}
            </div>
          )}

          {!readOnly && testEnv.status === 'ready' && testEnv.requirements_stale && (
            <div className="test-env-stale-notice">
              <p>Requirements changed since the last check.</p>
              <button
                className="btn btn-secondary"
                onClick={() => handleSubmit(testEnv.content)}
                disabled={busy}
              >
                {busy ? 'Checking…' : 'Re-check'}
              </button>
            </div>
          )}

          {!readOnly && (
            <div className="test-env-actions">
              {testEnv.status === 'ready' && (
                <button
                  className="btn btn-primary"
                  onClick={handleConfirm}
                  disabled={busy || testEnv.requirements_stale}
                >
                  Confirm
                </button>
              )}
              <button className="btn btn-secondary" onClick={startEditing} disabled={busy}>
                Edit
              </button>
            </div>
          )}
          {confirmed && active && (
            // `.test-env-cascade-notice` had no rule in any stylesheet, so this
            // has been rendering as unstyled body text since it was added.
            <p className="cascade-notice">
              Editing this description will delete every test plan in the sprint and require
              re-confirming. Existing test runs are kept, but marked as out of date.
            </p>
          )}
        </>
      )}

      {testEnv && (
        // Keyed on `updated_at`: a resubmit or an answered question re-runs
        // the extraction and can replace every variable, so an editor left
        // open would be holding a draft of values that no longer exist. The
        // key remounts it, which is what the page used to do by hand by
        // clearing the editor's flag from its own handlers.
        <EnvVarsEditor
          key={testEnv.updated_at}
          testEnv={testEnv}
          readOnly={readOnly}
          pageBusy={busy}
          onSaved={setTestEnv}
        />
      )}

      {testEnv?.status === 'confirmed' && (
        <div className="test-env-continue">
          <Link to={`/sprints/${sprintId}/test-plans`} className="btn btn-primary">
            Continue to Test Plans
          </Link>
        </div>
      )}

      {actionError && <p className="test-env-error">{actionError}</p>}
    </div>
  )
}
