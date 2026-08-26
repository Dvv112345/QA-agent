import { useState } from 'react'
import ModalShell from './ModalShell'
import { deleteCicdConfig, saveCicdConfig } from '../services/api'
import type { CicdConfig, CicdProvider, RepoResponse } from '../types'
import './CicdConfigModal.css'

interface Props {
  sprintId: number
  /** The current connection, or null when nothing is connected yet. */
  config: CicdConfig | null
  /** The sprint's registered repository — always the export destination. */
  repo: RepoResponse | null
  onSaved: (config: CicdConfig | null) => void
  onClose: () => void
}

/**
 * Connect the sprint to a CI system, or edit an existing connection.
 *
 * Deliberately has no repository field: the destination is always the
 * sprint's own registered repository, derived server-side from its GitHub
 * link. There is nothing here for a typo to redirect.
 */
export default function CicdConfigModal({ sprintId, config, repo, onSaved, onClose }: Props) {
  const [provider, setProvider] = useState<CicdProvider>(config?.provider ?? 'github_actions')
  const [accessToken, setAccessToken] = useState('')
  const [hint, setHint] = useState(config?.ci_environment_hint ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSave = () => {
    setBusy(true)
    setError(null)
    saveCicdConfig(sprintId, {
      provider,
      access_token: accessToken,
      ci_environment_hint: hint,
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
    deleteCicdConfig(sprintId)
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
    <ModalShell
      title={config ? 'CI/CD export' : 'Connect CI/CD export'}
      busy={busy}
      wide
      onClose={onClose}
    >
      <p className="cicd-config-hint">
        Verified test scripts are committed to{' '}
        {repo ? <strong>{repo.name}</strong> : 'this sprint’s repository'} as a pull request,
        alongside CI configuration written to match its existing conventions. Nothing is ever merged
        for you — the pull request is the deliverable.
      </p>

      <fieldset className="cicd-config-providers">
        <legend>CI system</legend>
        <label>
          <input
            type="radio"
            name="cicd-provider"
            value="github_actions"
            checked={provider === 'github_actions'}
            onChange={() => setProvider('github_actions')}
            disabled={busy}
          />
          GitHub Actions
        </label>
        <label>
          <input
            type="radio"
            name="cicd-provider"
            value="jenkins"
            checked={provider === 'jenkins'}
            onChange={() => setProvider('jenkins')}
            disabled={busy}
          />
          Jenkins
        </label>
      </fieldset>

      <label className="cicd-config-field">
        Access token
        <input
          type="password"
          value={accessToken}
          onChange={(e) => setAccessToken(e.target.value)}
          placeholder={
            config
              ? 'Leave blank to keep the current token'
              : repo?.has_access_token
                ? "Leave blank to use the repository's access token"
                : ''
          }
          disabled={busy}
        />
        <span className="cicd-config-field-note">
          Needs <strong>write</strong> access to contents and pull requests — this is checked
          against the repository before anything is saved. A read-only token is refused here rather
          than failing halfway through an export.
          {provider === 'github_actions' && (
            <>
              {' '}
              GitHub gates workflow files behind a separate grant, so an Actions export also needs
              the <code>workflow</code> scope (classic token) or{' '}
              <strong>Workflows: Read and write</strong> (fine-grained).
            </>
          )}
        </span>
      </label>

      <label className="cicd-config-field">
        CI environment notes <span className="cicd-config-optional">(optional)</span>
        <textarea
          value={hint}
          onChange={(e) => setHint(e.target.value)}
          rows={3}
          placeholder="e.g. runs on a self-hosted runner labelled qa; Postgres is available as a service container"
          disabled={busy}
        />
        <span className="cicd-config-field-note">
          Anything the generated job should know that the repository does not already say.
        </span>
      </label>

      {error && (
        <p className="cicd-config-error" role="alert">
          {error}
        </p>
      )}

      <div className="cicd-config-actions">
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
    </ModalShell>
  )
}
