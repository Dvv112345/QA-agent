import { useState } from 'react'
import { deleteIssueTracker, saveIssueTracker } from '../services/api'
import type { IssueTrackerConfig, IssueTrackerProvider, RepoResponse } from '../types'
import './IssueTrackerModal.css'

interface Props {
  sprintId: number
  /** The current connection, or null when nothing is connected yet. */
  config: IssueTrackerConfig | null
  /** The sprint's registered repository — the GitHub Issues shortcut. */
  repo: RepoResponse | null
  onSaved: (config: IssueTrackerConfig | null) => void
  onClose: () => void
}

/** Whether the sprint's own repo should be the GitHub target by default. */
function defaultUseSprintRepo(config: IssueTrackerConfig | null, repo: RepoResponse | null) {
  if (!repo) return false
  // A saved GitHub connection pointing somewhere else was a deliberate
  // choice; anything else (nothing saved, Jira saved, or a target that
  // already is this repo) starts on the sprint's own repository.
  if (config?.provider === 'github') return config.target === repo.name
  return true
}

export default function IssueTrackerModal({ sprintId, config, repo, onSaved, onClose }: Props) {
  const [provider, setProvider] = useState<IssueTrackerProvider>(config?.provider ?? 'jira')
  const [target, setTarget] = useState(config?.target ?? '')
  const [baseUrl, setBaseUrl] = useState(config?.base_url ?? '')
  const [accountEmail, setAccountEmail] = useState(config?.account_email ?? '')
  const [issueType, setIssueType] = useState(config?.issue_type ?? 'Bug')
  const [apiToken, setApiToken] = useState('')
  const [useSprintRepo, setUseSprintRepo] = useState(() => defaultUseSprintRepo(config, repo))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // The repository is derived server-side from the registered GitHub link;
  // this is the label only, so it never becomes the source of truth.
  const sprintRepo = provider === 'github' && repo ? repo : null
  const usingSprintRepo = sprintRepo !== null && useSprintRepo

  // Blank-means-keep applies only to a same-provider edit — the backend
  // rejects a switch with no token, since a Jira token cannot work for
  // GitHub. The placeholder says which rule is in force right now.
  const canKeepStoredToken = config !== null && config.provider === provider

  const switchProvider = (next: IssueTrackerProvider) => {
    if (next === provider) return
    setProvider(next)
    setError(null)
    // The target means something different per provider ("QA" vs
    // "acme/shop"), so carrying it across would only ever be wrong.
    setTarget(next === config?.provider ? (config?.target ?? '') : '')
    if (next === 'github') setUseSprintRepo(defaultUseSprintRepo(config, repo))
  }

  const handleSave = () => {
    setBusy(true)
    setError(null)
    saveIssueTracker(sprintId, {
      provider,
      // Blank when the box is ticked: the backend derives it from the
      // sprint's own repo rather than trusting what the form sends back.
      target: usingSprintRepo ? '' : target,
      base_url: provider === 'jira' ? baseUrl : null,
      account_email: provider === 'jira' ? accountEmail : null,
      issue_type: provider === 'jira' ? issueType : null,
      api_token: apiToken,
      use_sprint_repo: usingSprintRepo,
    })
      .then((saved) => {
        onSaved(saved)
        onClose()
      })
      .catch((err: Error) => {
        setError(err.message)
        setBusy(false)
      })
  }

  const handleDisconnect = () => {
    setBusy(true)
    setError(null)
    deleteIssueTracker(sprintId)
      .then(() => {
        onSaved(null)
        onClose()
      })
      .catch((err: Error) => {
        setError(err.message)
        setBusy(false)
      })
  }

  return (
    <div className="issue-tracker-overlay" role="dialog" aria-modal="true">
      <div className="issue-tracker-card">
        <h2>{config ? 'Issue tracker' : 'Connect an issue tracker'}</h2>
        <p className="issue-tracker-hint">
          Bug findings from a run can be filed here automatically. Credentials are checked against
          the tracker before anything is saved — though a token that can only read saves fine and
          fails at the first ticket, so it needs permission to write issues.
        </p>

        <fieldset className="issue-tracker-providers">
          <legend>Provider</legend>
          <label>
            <input
              type="radio"
              name="provider"
              value="jira"
              checked={provider === 'jira'}
              onChange={() => switchProvider('jira')}
              disabled={busy}
            />
            Jira
          </label>
          <label>
            <input
              type="radio"
              name="provider"
              value="github"
              checked={provider === 'github'}
              onChange={() => switchProvider('github')}
              disabled={busy}
            />
            GitHub Issues
          </label>
        </fieldset>

        {provider === 'jira' ? (
          <>
            <label className="issue-tracker-field">
              Jira site URL
              <input
                type="url"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://your-team.atlassian.net"
                disabled={busy}
              />
              <span className="issue-tracker-field-note">
                The site root only — for example <code>https://your-team.atlassian.net</code>. A URL
                with a path (<code>/jira</code>, a project page) is not the API root and will not
                verify.
              </span>
            </label>
            <label className="issue-tracker-field">
              Account email
              <input
                type="email"
                value={accountEmail}
                onChange={(e) => setAccountEmail(e.target.value)}
                placeholder="you@example.com"
                disabled={busy}
              />
            </label>
            <label className="issue-tracker-field">
              Project key
              <input
                type="text"
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                placeholder="QA"
                disabled={busy}
              />
            </label>
            <label className="issue-tracker-field">
              Issue type
              <input
                type="text"
                value={issueType}
                onChange={(e) => setIssueType(e.target.value)}
                placeholder="Bug"
                disabled={busy}
              />
            </label>
          </>
        ) : (
          <>
            {sprintRepo && (
              <div className="issue-tracker-sprint-repo">
                <label>
                  <input
                    type="checkbox"
                    checked={useSprintRepo}
                    onChange={(e) => setUseSprintRepo(e.target.checked)}
                    disabled={busy}
                  />
                  Use this sprint's repository — {sprintRepo.name}
                </label>
                <p className="issue-tracker-sprint-repo-note">
                  {sprintRepo.has_access_token
                    ? 'Its stored access token is used unless you enter one below.'
                    : 'This repository was registered without an access token — enter one below.'}
                </p>
              </div>
            )}
            {!usingSprintRepo && (
              <label className="issue-tracker-field">
                Repository
                <input
                  type="text"
                  value={target}
                  onChange={(e) => setTarget(e.target.value)}
                  placeholder="owner/repo"
                  disabled={busy}
                />
              </label>
            )}
          </>
        )}

        <label className="issue-tracker-field">
          API token
          <input
            type="password"
            value={apiToken}
            onChange={(e) => setApiToken(e.target.value)}
            placeholder={
              usingSprintRepo && sprintRepo?.has_access_token
                ? "Leave blank to use the repository's access token"
                : canKeepStoredToken
                  ? 'Leave blank to keep the current token'
                  : config
                    ? 'Required when changing provider'
                    : ''
            }
            disabled={busy}
          />
        </label>

        {error && <p className="issue-tracker-error">{error}</p>}

        <div className="issue-tracker-actions">
          <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
            {busy ? 'Checking…' : 'Save'}
          </button>
          <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          {config && (
            <button className="btn btn-danger" onClick={handleDisconnect} disabled={busy}>
              Disconnect
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
