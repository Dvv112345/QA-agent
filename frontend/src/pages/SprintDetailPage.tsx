import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import FinishSprintModal from '../components/FinishSprintModal'
import PrdUploadForm from '../components/PrdUploadForm'
import RequirementCard from '../components/RequirementCard'
import RequirementForm from '../components/RequirementForm'
import StageNav from '../components/StageNav'
import {
  confirmAllRequirements,
  fetchRequirements,
  fetchSprint,
  finishSprint,
} from '../services/api'
import type { RequirementResponse, SprintResponse } from '../types'
import { useCrumb } from '../BreadcrumbContext'
import { usePolling } from '../hooks/usePolling'
import { formatDate } from '../format'
import './SprintDetailPage.css'

// Statuses that still change without user input — worth polling for.
function isInProgress(requirement: RequirementResponse): boolean {
  return requirement.status === 'pending' || requirement.status === 'analyzing'
}

export default function SprintDetailPage() {
  const { id } = useParams<{ id: string }>()
  const sprintId = Number(id)
  const navigate = useNavigate()

  const [sprint, setSprint] = useState<SprintResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [finishing, setFinishing] = useState(false)
  const [confirmingFinish, setConfirmingFinish] = useState(false)
  const [requirements, setRequirements] = useState<RequirementResponse[]>([])
  const [requirementsError, setRequirementsError] = useState<string | null>(null)
  const [continuing, setContinuing] = useState(false)
  const [continueError, setContinueError] = useState<string | null>(null)
  const [confirmingAll, setConfirmingAll] = useState(false)
  const [confirmAllError, setConfirmAllError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchSprint(sprintId)
      .then((data) => {
        if (!cancelled) {
          setSprint(data)
          setLoading(false)
        }
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setError(err.message)
          setLoading(false)
        }
      })
    fetchRequirements(sprintId)
      .then((data) => {
        if (!cancelled) setRequirements(data)
      })
      .catch((err: Error) => {
        if (!cancelled) setRequirementsError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [sprintId])

  // Poll while any requirement is still queued or being analyzed.
  const shouldPoll = requirements.some(isInProgress)
  const confirmableCount = requirements.filter(
    (req) => req.status === 'ready' || req.status === 'needs_clarification',
  ).length

  usePolling(() => fetchRequirements(sprintId).then(setRequirements), { enabled: shouldPoll })

  useCrumb('sprint', sprint?.name)

  const handleFinish = () => {
    setFinishing(true)
    finishSprint(sprintId)
      .then((updated) => {
        setSprint(updated)
        setConfirmingFinish(false)
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setFinishing(false))
  }

  // The mount-time requirements_complete flag goes stale as the user
  // confirms requirements on this page — re-fetch before navigating.
  const handleContinue = () => {
    setContinuing(true)
    setContinueError(null)
    fetchSprint(sprintId)
      .then((fresh) => {
        setSprint(fresh)
        if (fresh.requirements_complete) {
          navigate(`/sprints/${sprintId}/test-environment`)
        } else {
          setContinueError('Confirm or delete the remaining requirements before continuing.')
        }
      })
      .catch((err: Error) => setContinueError(err.message))
      .finally(() => setContinuing(false))
  }

  // `requirements_complete` gates the control at the top of the page, and it
  // moves as the user works here. Every action that can move it refreshes the
  // sprint, or the gate would sit shut after the user had opened it.
  const refreshSprint = () =>
    fetchSprint(sprintId)
      .then(setSprint)
      .catch(() => {
        /* the rows already updated — the flag catches up on the next read */
      })

  // No confirmation: confirming is no longer final. A confirmed requirement
  // stays editable, and the cascade that an edit triggers is warned about at
  // the point of editing, which is where the consequence actually lives.
  const handleConfirmAll = () => {
    setConfirmingAll(true)
    setConfirmAllError(null)
    confirmAllRequirements(sprintId)
      .then((rows) => {
        setRequirements(rows)
        return refreshSprint()
      })
      .catch((err: Error) => setConfirmAllError(err.message))
      .finally(() => setConfirmingAll(false))
  }

  const handleSubmitted = (created: RequirementResponse[]) => {
    setRequirements((prev) => [...prev, ...created])
    void refreshSprint()
  }

  // A PRD upload replaces the previous upload's rows server-side — mirror
  // that locally: drop old from_prd rows, keep manual ones, append the new.
  const handlePrdUploaded = (created: RequirementResponse[]) => {
    setRequirements((prev) => [...prev.filter((req) => !req.from_prd), ...created])
    void refreshSprint()
  }

  const handleUpdated = (updated: RequirementResponse) => {
    setRequirements((prev) => prev.map((req) => (req.id === updated.id ? updated : req)))
    void refreshSprint()
  }

  const handleRemoved = (removedId: number) => {
    setRequirements((prev) => prev.filter((req) => req.id !== removedId))
    void refreshSprint()
  }

  if (loading) return <p className="sprint-detail-message">Loading sprint&hellip;</p>
  if (error) return <p className="sprint-detail-message sprint-detail-error">{error}</p>
  if (!sprint) return <p className="sprint-detail-message">Sprint not found.</p>

  const repo = sprint.repo
  const analyzedCount = requirements.filter((req) => !isInProgress(req)).length
  // Whether editing or removing a requirement now destroys something — a test
  // plan, or the environment's confirmed state. `has_test_plans` is not
  // redundant with `environment_confirmed`: adding a requirement un-confirms
  // the environment without removing plans, so plans can outlive confirmation.
  const editingCascades = sprint.environment_confirmed || sprint.has_test_plans

  return (
    <div className="sprint-detail">
      <nav className="page-back">
        <Link to="/" className="back-link">
          &larr; Back to Sprints
        </Link>
      </nav>

      <StageNav
        to={`/sprints/${sprintId}/test-environment`}
        label="Test Environment"
        ready={sprint.requirements_complete}
        blockedReason="Confirm every requirement to continue."
      />

      <header className="sprint-detail-header">
        <h1>{sprint.name}</h1>
        <span className={`badge ${sprint.active ? 'badge-active' : 'badge-finished'}`}>
          {sprint.active ? 'Active' : 'Finished'}
        </span>
      </header>

      <time className="sprint-detail-date">Created {formatDate(sprint.created_at)}</time>

      {repo && (
        <section className="repo-info-card">
          <h2>Repository</h2>
          <dl>
            <dt>Name</dt>
            <dd>{repo.name}</dd>
            {repo.description && (
              <>
                <dt>Description</dt>
                <dd>{repo.description}</dd>
              </>
            )}
            <dt>GitHub</dt>
            <dd>
              <a href={repo.github_link} target="_blank" rel="noopener noreferrer">
                {repo.github_link}
              </a>
            </dd>
          </dl>
        </section>
      )}

      <section className="requirements-section">
        <h2>Requirements</h2>

        {requirementsError && <p className="sprint-detail-error">{requirementsError}</p>}

        {requirements.length > 0 && (
          <p className="requirements-summary">
            {analyzedCount} of {requirements.length} analyzed
          </p>
        )}

        {sprint.active && requirements.length > 0 && (
          <div className="requirements-confirm-all">
            <button
              className="btn btn-primary"
              onClick={handleConfirmAll}
              disabled={confirmingAll || shouldPoll || confirmableCount === 0}
            >
              {confirmingAll ? 'Confirming…' : `Confirm all (${confirmableCount})`}
            </button>
            {shouldPoll && (
              <p className="requirements-confirm-all-hint">
                Waiting for analysis to finish&hellip;
              </p>
            )}
            {confirmAllError && <p className="sprint-detail-error">{confirmAllError}</p>}
          </div>
        )}

        {requirements.map((requirement) => (
          <RequirementCard
            key={requirement.id}
            requirement={requirement}
            sprintActive={sprint.active}
            cascades={editingCascades}
            onUpdated={handleUpdated}
            onRemoved={handleRemoved}
          />
        ))}

        {sprint.active && (
          <>
            <PrdUploadForm
              sprintId={sprintId}
              hasPrdRequirements={requirements.some((req) => req.from_prd)}
              onUploaded={handlePrdUploaded}
            />
            <RequirementForm sprintId={sprintId} onSubmitted={handleSubmitted} />
          </>
        )}
      </section>

      {sprint.active && requirements.length > 0 && (
        <div className="sprint-detail-continue">
          <button className="btn btn-primary" disabled={continuing} onClick={handleContinue}>
            {continuing ? 'Checking…' : 'Continue'}
          </button>
          {continueError && <p className="sprint-detail-error">{continueError}</p>}
        </div>
      )}

      {sprint.active && (
        <button
          className="btn btn-danger"
          disabled={finishing}
          onClick={() => setConfirmingFinish(true)}
        >
          Finish Sprint
        </button>
      )}

      {confirmingFinish && (
        <FinishSprintModal
          sprintName={sprint.name}
          busy={finishing}
          onConfirm={handleFinish}
          onCancel={() => setConfirmingFinish(false)}
        />
      )}
    </div>
  )
}
