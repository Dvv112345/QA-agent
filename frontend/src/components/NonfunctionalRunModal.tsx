import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { createNonfunctionalRun, generateNonfunctionalPlan } from '../services/api'
import type {
  IssueTrackerConfig,
  LoadLimits,
  LoadMethod,
  LoadProfileDraft,
  LoadShape,
  NonfunctionalDomain,
  NonfunctionalPlanDraftResponse,
  TestPlanResponse,
} from '../types'
import { LOAD_METHODS, LOAD_SHAPES, NONFUNCTIONAL_DOMAINS } from '../types'
import { LOAD_SHAPE_DESCRIPTIONS, LOAD_SHAPE_LABELS } from '../statusLabels'
import ModalShell from './ModalShell'
import { plural } from '../format'
import './NonfunctionalRunModal.css'

interface Props {
  sprintId: number
  /** The sprint's approved plans — see ExploratoryCharterModal for why. */
  plans: TestPlanResponse[]
  /** `SprintResponse.load_limits` — the server's maximums, never a literal here. */
  limits: LoadLimits
  tracker?: IssueTrackerConfig | null
  onClose: () => void
}

const DOMAIN_LABELS: Record<NonfunctionalDomain, string> = {
  accessibility: 'Accessibility',
  performance: 'Performance',
  security: 'Security',
}

const count = (value: number) => value.toLocaleString('en-US')

/** A whole number within [1, max], or null when the field says anything else. */
function parseCeiling(text: string, max: number): number | null {
  const value = Number(text)
  return text.trim() !== '' && Number.isInteger(value) && value >= 1 && value <= max ? value : null
}

interface CeilingFieldsProps {
  limits: LoadLimits
  maxUsersText: string
  maxTotalText: string
  disposable: boolean
  busy: boolean
  onMaxUsers: (text: string) => void
  onMaxTotal: (text: string) => void
  onDisposable: (value: boolean) => void
}

/**
 * The run ceiling and the disposable declaration. Shown before generation, so
 * the model sizes its proposals against them, and again on the review step,
 * where they stay editable.
 */
function CeilingFields({
  limits,
  maxUsersText,
  maxTotalText,
  disposable,
  busy,
  onMaxUsers,
  onMaxTotal,
  onDisposable,
}: CeilingFieldsProps) {
  return (
    <fieldset className="nf-ceiling">
      <legend>Run ceiling</legend>
      <div className="nf-ceiling-inputs">
        <label className="nf-profile-number">
          Peak users
          <input
            type="number"
            min={1}
            max={limits.max_users}
            value={maxUsersText}
            onChange={(e) => onMaxUsers(e.target.value)}
            disabled={busy}
          />
        </label>
        <label className="nf-profile-number">
          Total requests for this run
          <input
            type="number"
            min={1}
            max={limits.max_total_requests}
            value={maxTotalText}
            onChange={(e) => onMaxTotal(e.target.value)}
            disabled={busy}
          />
        </label>
      </div>
      <label className="nf-disposable">
        <input
          type="checkbox"
          checked={disposable}
          onChange={(e) => onDisposable(e.target.checked)}
          disabled={busy}
        />
        This environment is disposable — its data can be changed or destroyed
      </label>
      <p className="nf-warning">
        Load profiles run <strong>as the signed-in browser user</strong>, carrying its cookies.
        Methods that change data (POST, PUT, PATCH, DELETE) need the declaration above, and then run
        under the same ceiling — every request they send can be a write.
      </p>
    </fieldset>
  )
}

export default function NonfunctionalRunModal({
  sprintId,
  plans,
  limits,
  tracker,
  onClose,
}: Props) {
  const navigate = useNavigate()
  const [selected, setSelected] = useState<number | null>(plans[0]?.requirement_id ?? null)
  // Pre-filled with the server's maximums; the user narrows them.
  const [maxUsersText, setMaxUsersText] = useState(String(limits.max_users))
  const [maxTotalText, setMaxTotalText] = useState(String(limits.max_total_requests))
  const [disposable, setDisposable] = useState(false)
  const [draft, setDraft] = useState<NonfunctionalPlanDraftResponse | null>(null)
  const [domains, setDomains] = useState<NonfunctionalDomain[]>([])
  const [profiles, setProfiles] = useState<LoadProfileDraft[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // See RunTestModal — checked by default when a tracker is connected.
  const [exportFindings, setExportFindings] = useState(Boolean(tracker))

  const maxUsers = parseCeiling(maxUsersText, limits.max_users)
  const maxTotal = parseCeiling(maxTotalText, limits.max_total_requests)
  const ceilingReason =
    maxUsers === null
      ? `Peak users must be a whole number from 1 to ${count(limits.max_users)}.`
      : maxTotal === null
        ? `Total requests must be a whole number from 1 to ${count(limits.max_total_requests)}.`
        : null

  const handleGenerate = () => {
    if (selected === null || maxUsers === null || maxTotal === null) return
    setBusy(true)
    setError(null)
    generateNonfunctionalPlan(
      sprintId,
      selected,
      { max_users: maxUsers, max_total_requests: maxTotal },
      disposable,
    )
      .then((data) => {
        setDraft(data)
        setDomains(data.domains.filter((d) => d.applicable).map((d) => d.domain))
        setProfiles(data.load_profiles)
        setBusy(false)
      })
      .catch((err: Error) => {
        setError(err.message)
        setBusy(false)
      })
  }

  const handleStart = () => {
    if (draft === null || maxUsers === null || maxTotal === null) return
    setBusy(true)
    setError(null)
    createNonfunctionalRun(
      sprintId,
      draft.requirement_id,
      domains,
      draft.base_url_env_vars,
      profiles,
      disposable,
      { max_users: maxUsers, max_total_requests: maxTotal },
      exportFindings,
    )
      .then((run) => {
        navigate(`/sprints/${sprintId}/nonfunctional-runs/${run.id}`)
      })
      .catch((err: Error) => {
        setError(err.message)
        setBusy(false)
      })
  }

  const toggleDomain = (domain: NonfunctionalDomain) => {
    setDomains((prev) =>
      prev.includes(domain) ? prev.filter((d) => d !== domain) : [...prev, domain],
    )
  }

  const updateProfile = (index: number, patch: Partial<LoadProfileDraft>) => {
    setProfiles((prev) => prev.map((p, i) => (i === index ? { ...p, ...patch } : p)))
  }

  const removeProfile = (index: number) => {
    setProfiles((prev) => prev.filter((_, i) => i !== index))
  }

  const addProfile = () => {
    if (draft === null) return
    setProfiles((prev) => {
      const left = (maxTotal ?? 0) - prev.reduce((sum, p) => sum + p.total_request_cap, 0)
      return [
        ...prev,
        {
          url: '',
          method: 'GET',
          body: null,
          shape: 'load',
          concurrency: 1,
          duration_seconds: 10,
          total_request_cap: Math.max(1, Math.min(50, left)),
          rationale: '',
        },
      ]
    })
  }

  // Everything below is the server's rule restated for feedback, never as the
  // guarantee: the create route checks each of these again (Convention #10
  // keeps every number here coming from `limits`).
  const allocated = profiles.reduce((sum, profile) => sum + profile.total_request_cap, 0)
  const overBudget = maxTotal !== null && allocated > maxTotal
  const shortStress = profiles.some(
    (profile) =>
      profile.shape === 'stress' && profile.duration_seconds < limits.stress_min_duration_seconds,
  )
  const unsafeSelected = profiles.some((profile) => !limits.safe_methods.includes(profile.method))
  const canStart =
    domains.length > 0 &&
    ceilingReason === null &&
    profiles.every((profile) => profile.url.trim().length > 0) &&
    !shortStress &&
    !overBudget &&
    (!unsafeSelected || disposable)
  // Every clause of `canStart`, in the same order, so a disabled Start
  // button always says which one is holding it.
  const blockedReason =
    domains.length === 0
      ? 'Select at least one check to run.'
      : ceilingReason !== null
        ? ceilingReason
        : profiles.some((profile) => profile.url.trim().length === 0)
          ? 'Every load profile needs a URL, or remove it.'
          : shortStress
            ? `A stress profile needs at least ${limits.stress_min_duration_seconds} seconds.`
            : overBudget
              ? `Load profiles request ${count(allocated)} in total; this run's budget is ${count(maxTotal ?? 0)}.`
              : 'A load profile uses a method that changes data — declare the environment disposable, or switch it to GET, HEAD or OPTIONS.'

  const methodAllowed = (method: LoadMethod) => limits.safe_methods.includes(method) || disposable
  const secondsMax = (shape: LoadShape) =>
    shape === 'soak' ? limits.max_soak_duration_seconds : limits.max_duration_seconds

  const ceilingFields = (
    <CeilingFields
      limits={limits}
      maxUsersText={maxUsersText}
      maxTotalText={maxTotalText}
      disposable={disposable}
      busy={busy}
      onMaxUsers={setMaxUsersText}
      onMaxTotal={setMaxTotalText}
      onDisposable={setDisposable}
    />
  )

  return (
    <ModalShell title="Start nonfunctional testing" busy={busy} wide onClose={onClose}>
      {plans.length === 0 ? (
        <p className="nf-message">No requirements have an approved test plan yet.</p>
      ) : draft === null ? (
        <>
          <p className="nf-hint">
            A nonfunctional run covers one requirement. It walks the feature and runs the checks you
            select at every page and endpoint it reaches.
          </p>
          <ul className="nf-requirement-list">
            {plans.map((plan) => (
              <li key={plan.requirement_id}>
                <label>
                  <input
                    type="radio"
                    name="nonfunctional-requirement"
                    checked={selected === plan.requirement_id}
                    onChange={() => setSelected(plan.requirement_id)}
                    disabled={busy}
                  />
                  {plan.requirement_name}
                </label>
              </li>
            ))}
          </ul>
          {ceilingFields}
        </>
      ) : (
        <>
          <p className="nf-hint">
            Review what will run against <strong>{draft.requirement_name}</strong>.
          </p>
          <p className="nf-urls">
            The walk starts at <strong>{draft.base_url_env_vars[0]}</strong>
            {draft.base_url_env_vars.length > 1 &&
              `; also reachable: ${draft.base_url_env_vars.slice(1).join(', ')}`}
          </p>

          {ceilingFields}

          <section className="nf-section">
            <h3>Checks</h3>
            <ul className="nf-domain-list">
              {NONFUNCTIONAL_DOMAINS.map((domain) => {
                const proposal = draft.domains.find((d) => d.domain === domain)
                return (
                  <li key={domain} className="nf-domain">
                    <label>
                      <input
                        type="checkbox"
                        checked={domains.includes(domain)}
                        onChange={() => toggleDomain(domain)}
                        disabled={busy}
                      />
                      {DOMAIN_LABELS[domain]}
                    </label>
                    {proposal && <p className="nf-domain-rationale">{proposal.rationale}</p>}
                  </li>
                )
              })}
            </ul>
          </section>

          <section className="nf-section">
            <h3>Load profiles</h3>

            {profiles.length === 0 ? (
              <p className="nf-empty">No load profiles — the run will only examine pages.</p>
            ) : (
              <>
                <p className={overBudget ? 'nf-allocation nf-allocation-over' : 'nf-allocation'}>
                  {count(allocated)} of {maxTotal === null ? '—' : count(maxTotal)} requests
                  allocated
                </p>
                <ul className="nf-profile-list">
                  {profiles.map((profile, index) => (
                    <li key={index} className="nf-profile">
                      <div className="nf-profile-row">
                        <select
                          className="nf-profile-method"
                          value={profile.method}
                          onChange={(e) =>
                            updateProfile(index, { method: e.target.value as LoadMethod })
                          }
                          disabled={busy}
                          aria-label={`Method for profile ${index + 1}`}
                        >
                          {LOAD_METHODS.map((method) => (
                            <option key={method} value={method} disabled={!methodAllowed(method)}>
                              {method}
                              {!methodAllowed(method) ? ' (needs declaration)' : ''}
                            </option>
                          ))}
                        </select>
                        <select
                          className="nf-profile-method"
                          value={profile.shape}
                          onChange={(e) =>
                            updateProfile(index, { shape: e.target.value as LoadShape })
                          }
                          disabled={busy}
                          aria-label={`Shape for profile ${index + 1}`}
                        >
                          {LOAD_SHAPES.map((shape) => (
                            <option key={shape} value={shape}>
                              {LOAD_SHAPE_LABELS[shape]}
                            </option>
                          ))}
                        </select>
                        <input
                          className="nf-profile-url"
                          type="text"
                          value={profile.url}
                          onChange={(e) => updateProfile(index, { url: e.target.value })}
                          disabled={busy}
                          placeholder="https://…"
                          aria-label={`URL for profile ${index + 1}`}
                        />
                        <button
                          type="button"
                          className="btn btn-secondary btn-small"
                          onClick={() => removeProfile(index)}
                          disabled={busy}
                        >
                          Remove
                        </button>
                      </div>
                      <p className="nf-shape-description">
                        {LOAD_SHAPE_DESCRIPTIONS[profile.shape]}
                      </p>
                      <div className="nf-profile-row">
                        {profile.shape === 'stress' ? (
                          // A stress profile has no users of its own: it
                          // always ramps to the run's peak.
                          <span className="nf-shape-note">
                            Ramps in steps up to {plural(maxUsers ?? limits.max_users, 'user')}
                          </span>
                        ) : (
                          <label className="nf-profile-number">
                            Users
                            <input
                              type="number"
                              min={1}
                              max={maxUsers ?? limits.max_users}
                              value={profile.concurrency}
                              onChange={(e) =>
                                updateProfile(index, { concurrency: Number(e.target.value) })
                              }
                              disabled={busy}
                            />
                          </label>
                        )}
                        <label className="nf-profile-number">
                          Seconds
                          <input
                            type="number"
                            min={
                              profile.shape === 'stress' ? limits.stress_min_duration_seconds : 1
                            }
                            max={secondsMax(profile.shape)}
                            value={profile.duration_seconds}
                            onChange={(e) =>
                              updateProfile(index, { duration_seconds: Number(e.target.value) })
                            }
                            disabled={busy}
                          />
                        </label>
                        <label className="nf-profile-number">
                          Total requests
                          <input
                            type="number"
                            min={1}
                            // What the budget has left, plus this profile's own share.
                            max={Math.max(
                              1,
                              (maxTotal ?? 0) - allocated + profile.total_request_cap,
                            )}
                            value={profile.total_request_cap}
                            onChange={(e) =>
                              updateProfile(index, { total_request_cap: Number(e.target.value) })
                            }
                            disabled={busy}
                          />
                        </label>
                      </div>
                      {profile.rationale && (
                        <p className="nf-profile-rationale">{profile.rationale}</p>
                      )}
                    </li>
                  ))}
                </ul>
              </>
            )}
            <button
              type="button"
              className="btn btn-secondary"
              onClick={addProfile}
              disabled={busy}
            >
              Add load profile
            </button>
          </section>
        </>
      )}

      {draft !== null && (
        <label className="nf-export">
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

      {draft === null && ceilingReason !== null && !busy && (
        <p className="nf-blocked">{ceilingReason}</p>
      )}
      {draft !== null && !canStart && !busy && <p className="nf-blocked">{blockedReason}</p>}
      {error && <p className="nf-error">{error}</p>}

      <div className="nf-actions">
        {draft === null ? (
          <button
            className="btn btn-primary"
            onClick={handleGenerate}
            disabled={busy || selected === null || plans.length === 0 || ceilingReason !== null}
          >
            {busy ? 'Preparing…' : 'Prepare run'}
          </button>
        ) : (
          <button className="btn btn-primary" onClick={handleStart} disabled={busy || !canStart}>
            {busy ? 'Starting…' : `Start run (${plural(domains.length, 'check')})`}
          </button>
        )}
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
      </div>
    </ModalShell>
  )
}
