import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import PrdUploadForm from '../components/PrdUploadForm'
import RequirementCard from '../components/RequirementCard'
import RequirementForm from '../components/RequirementForm'
import {
  confirmAllRequirements,
  fetchRequirements,
  fetchSprint,
  finishSprint,
} from '../services/api'
import type { RequirementResponse, SprintResponse } from '../types'
import './SprintDetailPage.css'

const POLL_INTERVAL_MS = 2500

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
  const [requirements, setRequirements] = useState<RequirementResponse[]>([])
  const [requirementsError, setRequirementsError] = useState<string | null>(null)
  const [continuing, setContinuing] = useState(false)
  const [continueError, setContinueError] = useState<string | null>(null)
  const [confirmingAll, setConfirmingAll] = useState(false)
  const [confirmAllError, setConfirmAllError] = useState<string | null>(null)

  const fetchingRef = useRef(false)

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

  useEffect(() => {
    if (!shouldPoll) return

    const pollId = setInterval(() => {
      if (fetchingRef.current) return
      fetchingRef.current = true
      fetchRequirements(sprintId)
        .then(setRequirements)
        .catch(() => {
          /* transient poll failure — retry on next tick */
        })
        .finally(() => {
          fetchingRef.current = false
        })
    }, POLL_INTERVAL_MS)

    return () => clearInterval(pollId)
  }, [shouldPoll, sprintId])

  const handleFinish = () => {
    setFinishing(true)
    finishSprint(sprintId)
      .then(setSprint)
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

  const handleConfirmAll = () => {
    if (
      !window.confirm(
        `Confirm all ${confirmableCount} requirement(s)? This includes any still awaiting clarification. This is final.`,
      )
    )
      return
    setConfirmingAll(true)
    setConfirmAllError(null)
    confirmAllRequirements(sprintId)
      .then(setRequirements)
      .catch((err: Error) => setConfirmAllError(err.message))
      .finally(() => setConfirmingAll(false))
  }

  const handleSubmitted = (created: RequirementResponse[]) => {
    setRequirements((prev) => [...prev, ...created])
  }

  // A PRD upload replaces the previous upload's rows server-side — mirror
  // that locally: drop old from_prd rows, keep manual ones, append the new.
  const handlePrdUploaded = (created: RequirementResponse[]) => {
    setRequirements((prev) => [...prev.filter((req) => !req.from_prd), ...created])
  }

  const handleUpdated = (updated: RequirementResponse) => {
    setRequirements((prev) => prev.map((req) => (req.id === updated.id ? updated : req)))
  }

  const handleRemoved = (removedId: number) => {
    setRequirements((prev) => prev.filter((req) => req.id !== removedId))
  }

  if (loading) return <p className="sprint-detail-message">Loading sprint&hellip;</p>
  if (error) return <p className="sprint-detail-message sprint-detail-error">{error}</p>
  if (!sprint) return <p className="sprint-detail-message">Sprint not found.</p>

  const repo = sprint.repo
  const analyzedCount = requirements.filter((req) => !isInProgress(req)).length

  return (
    <div className="sprint-detail">
      <Link to="/" className="back-link">
        &larr; Back to Sprints
      </Link>

      <header className="sprint-detail-header">
        <h1>{sprint.name}</h1>
        <span className={`badge ${sprint.active ? 'badge-active' : 'badge-finished'}`}>
          {sprint.active ? 'Active' : 'Finished'}
        </span>
      </header>

      <time className="sprint-detail-date">
        Created {new Date(sprint.created_at).toLocaleDateString()}
      </time>

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
            locked={sprint.requirements_locked}
            onUpdated={handleUpdated}
            onRemoved={handleRemoved}
          />
        ))}

        {sprint.active && !sprint.requirements_locked && (
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
        <button className="btn btn-danger" disabled={finishing} onClick={handleFinish}>
          {finishing ? 'Finishing…' : 'Finish Sprint'}
        </button>
      )}
    </div>
  )
}
