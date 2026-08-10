import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { createExploratoryRun, generateCharters } from '../services/api'
import type {
  CharterDraft,
  ExploratoryCharterDraftResponse,
  IssueTrackerConfig,
  SfdipotArea,
  TestPlanResponse,
} from '../types'
import { SFDIPOT_AREAS } from '../types'
import { plural } from '../format'
import './ExploratoryCharterModal.css'

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

export default function ExploratoryCharterModal({ sprintId, plans, tracker, onClose }: Props) {
  const navigate = useNavigate()
  const [selected, setSelected] = useState<number | null>(plans[0]?.requirement_id ?? null)
  const [draft, setDraft] = useState<ExploratoryCharterDraftResponse | null>(null)
  const [charters, setCharters] = useState<CharterDraft[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // See RunTestModal — checked by default when a tracker is connected.
  const [exportFindings, setExportFindings] = useState(Boolean(tracker))

  const handleGenerate = () => {
    if (selected === null) return
    setBusy(true)
    setError(null)
    generateCharters(sprintId, selected)
      .then((data) => {
        setDraft(data)
        setCharters(data.charters)
        setBusy(false)
      })
      .catch((err: Error) => {
        setError(err.message)
        setBusy(false)
      })
  }

  const handleStart = () => {
    if (draft === null) return
    setBusy(true)
    setError(null)
    createExploratoryRun(
      sprintId,
      draft.requirement_id,
      charters,
      draft.base_url_env_vars,
      exportFindings,
    )
      .then((run) => {
        navigate(`/sprints/${sprintId}/exploratory-runs/${run.id}`)
      })
      .catch((err: Error) => {
        setError(err.message)
        setBusy(false)
      })
  }

  const updateCharter = (index: number, text: string) => {
    setCharters((prev) => prev.map((c, i) => (i === index ? { ...c, charter: text } : c)))
  }

  const toggleArea = (index: number, area: SfdipotArea) => {
    setCharters((prev) =>
      prev.map((c, i) =>
        i === index
          ? {
              ...c,
              sfdipot_areas: c.sfdipot_areas.includes(area)
                ? c.sfdipot_areas.filter((a) => a !== area)
                : [...c.sfdipot_areas, area],
            }
          : c,
      ),
    )
  }

  const removeCharter = (index: number) => {
    setCharters((prev) => prev.filter((_, i) => i !== index))
  }

  const addCharter = () => {
    setCharters((prev) => [...prev, { charter: '', sfdipot_areas: [] }])
  }

  const canStart = charters.length > 0 && charters.every((c) => c.charter.trim().length > 0)

  // Scale the server's estimate by how many charters remain after editing.
  // Derived purely from values the server sent — no config constant is
  // duplicated here (Convention #10).
  const projectedMinutes =
    draft === null || draft.charter_count === 0
      ? 0
      : Math.max(1, Math.round((draft.projected_minutes / draft.charter_count) * charters.length))

  return (
    <div className="charter-overlay" role="dialog" aria-modal="true">
      <div className="charter-card">
        <h2>Start exploratory testing</h2>

        {plans.length === 0 ? (
          <p className="charter-message">No requirements have an approved test plan yet.</p>
        ) : draft === null ? (
          <>
            <p className="charter-hint">
              Exploration covers one requirement at a time. Pick the one to explore.
            </p>
            <ul className="charter-requirement-list">
              {plans.map((plan) => (
                <li key={plan.requirement_id}>
                  <label>
                    <input
                      type="radio"
                      name="exploratory-requirement"
                      checked={selected === plan.requirement_id}
                      onChange={() => setSelected(plan.requirement_id)}
                      disabled={busy}
                    />
                    {plan.requirement_name}
                  </label>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <>
            <p className="charter-hint">
              Review the charters for <strong>{draft.requirement_name}</strong>. Each becomes one
              time-boxed session.
            </p>
            {/* The first variable is where each session's browser opens, so
                name it rather than showing an undifferentiated list — the
                order is part of what the user is approving here. */}
            <p className="charter-urls">
              Exploration starts at <strong>{draft.base_url_env_vars[0]}</strong>
              {draft.base_url_env_vars.length > 1 &&
                `; also reachable: ${draft.base_url_env_vars.slice(1).join(', ')}`}
            </p>
            <ul className="charter-list">
              {charters.map((charter, index) => (
                <li key={index} className="charter-item">
                  <textarea
                    className="charter-text"
                    value={charter.charter}
                    onChange={(e) => updateCharter(index, e.target.value)}
                    disabled={busy}
                    rows={2}
                    aria-label={`Charter ${index + 1}`}
                  />
                  <div className="charter-areas">
                    {SFDIPOT_AREAS.map((area) => (
                      <label key={area} className="charter-area">
                        <input
                          type="checkbox"
                          checked={charter.sfdipot_areas.includes(area)}
                          onChange={() => toggleArea(index, area)}
                          disabled={busy}
                        />
                        {area}
                      </label>
                    ))}
                  </div>
                  <button
                    type="button"
                    className="btn btn-secondary charter-remove"
                    onClick={() => removeCharter(index)}
                    disabled={busy}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={addCharter}
              disabled={busy}
            >
              Add charter
            </button>
          </>
        )}

        {draft !== null && (
          <label className="charter-export">
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
        )}

        {error && <p className="charter-error">{error}</p>}

        <div className="charter-actions">
          {draft === null ? (
            <button
              className="btn btn-primary"
              onClick={handleGenerate}
              disabled={busy || selected === null || plans.length === 0}
            >
              {busy ? 'Generating…' : 'Generate charters'}
            </button>
          ) : (
            <button className="btn btn-primary" onClick={handleStart} disabled={busy || !canStart}>
              {busy
                ? 'Starting…'
                : `Start ${plural(charters.length, 'session')} (~${projectedMinutes} min)`}
            </button>
          )}
          <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
