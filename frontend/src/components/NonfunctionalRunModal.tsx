import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { createNonfunctionalRun, generateNonfunctionalPlan } from '../services/api'
import type {
  IssueTrackerConfig,
  LoadMethod,
  LoadProfileDraft,
  NonfunctionalDomain,
  NonfunctionalPlanDraftResponse,
  TestPlanResponse,
} from '../types'
import { LOAD_METHODS, NONFUNCTIONAL_DOMAINS } from '../types'
import ModalShell from './ModalShell'
import { plural } from '../format'
import './NonfunctionalRunModal.css'

interface Props {
  sprintId: number
  /** The sprint's approved plans — see ExploratoryCharterModal for why. */
  plans: TestPlanResponse[]
  tracker?: IssueTrackerConfig | null
  onClose: () => void
}

const DOMAIN_LABELS: Record<NonfunctionalDomain, string> = {
  accessibility: 'Accessibility',
  performance: 'Performance',
  security: 'Security',
}

export default function NonfunctionalRunModal({ sprintId, plans, tracker, onClose }: Props) {
  const navigate = useNavigate()
  const [selected, setSelected] = useState<number | null>(plans[0]?.requirement_id ?? null)
  const [draft, setDraft] = useState<NonfunctionalPlanDraftResponse | null>(null)
  const [domains, setDomains] = useState<NonfunctionalDomain[]>([])
  const [profiles, setProfiles] = useState<LoadProfileDraft[]>([])
  const [disposable, setDisposable] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // See RunTestModal — checked by default when a tracker is connected.
  const [exportFindings, setExportFindings] = useState(Boolean(tracker))

  const handleGenerate = () => {
    if (selected === null) return
    setBusy(true)
    setError(null)
    generateNonfunctionalPlan(sprintId, selected)
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
    if (draft === null) return
    setBusy(true)
    setError(null)
    createNonfunctionalRun(
      sprintId,
      draft.requirement_id,
      domains,
      draft.base_url_env_vars,
      profiles,
      disposable,
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
    setProfiles((prev) => [
      ...prev,
      {
        url: '',
        method: 'GET',
        body: null,
        concurrency: 1,
        duration_seconds: 10,
        total_request_cap: 50,
        rationale: '',
      },
    ])
  }

  // Ceilings come from the server, never from a config literal restated
  // here (Convention #10). The declaration selects the tier.
  const maxConcurrency =
    draft === null
      ? 1
      : disposable
        ? Math.max(draft.max_concurrency, draft.unsafe_max_concurrency)
        : draft.max_concurrency
  const unsafeSelected = profiles.some(
    (profile) => draft !== null && !draft.safe_methods.includes(profile.method),
  )
  const canStart =
    domains.length > 0 &&
    profiles.every((profile) => profile.url.trim().length > 0) &&
    (!unsafeSelected || disposable)
  // Every clause of `canStart`, in the same order, so a disabled Start
  // button always says which one is holding it.
  const blockedReason =
    domains.length === 0
      ? 'Select at least one check to run.'
      : profiles.some((profile) => profile.url.trim().length === 0)
        ? 'Every load profile needs a URL, or remove it.'
        : 'A load profile uses a method that changes data — declare the environment disposable, or switch it to GET, HEAD or OPTIONS.'

  const methodAllowed = (method: LoadMethod) =>
    draft !== null && (draft.safe_methods.includes(method) || disposable)

  const ceilingFor = (method: LoadMethod) => {
    if (draft === null) return { requests: 0, concurrency: 0 }
    return draft.safe_methods.includes(method)
      ? { requests: draft.max_total_requests, concurrency: draft.max_concurrency }
      : { requests: draft.unsafe_max_total_requests, concurrency: draft.unsafe_max_concurrency }
  }

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
            <label className="nf-disposable">
              <input
                type="checkbox"
                checked={disposable}
                onChange={(e) => setDisposable(e.target.checked)}
                disabled={busy}
              />
              This environment is disposable — its data can be changed or destroyed
            </label>
            <p className="nf-warning">
              Load profiles run <strong>as the signed-in browser user</strong>, carrying its
              cookies. Methods that change data (POST, PUT, PATCH, DELETE) need the declaration
              above, and are capped at {plural(draft.unsafe_max_total_requests, 'request')} in
              total.
            </p>

            {profiles.length === 0 ? (
              <p className="nf-empty">No load profiles — the run will only examine pages.</p>
            ) : (
              <ul className="nf-profile-list">
                {profiles.map((profile, index) => {
                  const ceiling = ceilingFor(profile.method)
                  return (
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
                      <div className="nf-profile-row">
                        <label className="nf-profile-number">
                          Concurrency
                          <input
                            type="number"
                            min={1}
                            max={maxConcurrency}
                            value={profile.concurrency}
                            onChange={(e) =>
                              updateProfile(index, { concurrency: Number(e.target.value) })
                            }
                            disabled={busy}
                          />
                        </label>
                        <label className="nf-profile-number">
                          Seconds
                          <input
                            type="number"
                            min={1}
                            max={draft.max_duration_seconds}
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
                            max={ceiling.requests}
                            value={profile.total_request_cap}
                            onChange={(e) =>
                              updateProfile(index, { total_request_cap: Number(e.target.value) })
                            }
                            disabled={busy}
                          />
                        </label>
                        <span className="nf-profile-ceiling">
                          max {ceiling.concurrency} × {ceiling.requests}
                        </span>
                      </div>
                      {profile.rationale && (
                        <p className="nf-profile-rationale">{profile.rationale}</p>
                      )}
                    </li>
                  )
                })}
              </ul>
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

      {draft !== null && !canStart && !busy && <p className="nf-blocked">{blockedReason}</p>}
      {error && <p className="nf-error">{error}</p>}

      <div className="nf-actions">
        {draft === null ? (
          <button
            className="btn btn-primary"
            onClick={handleGenerate}
            disabled={busy || selected === null || plans.length === 0}
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
