import { useState } from 'react'
import { updateTestEnvironmentVars } from '../services/api'
import type { TestEnvironmentResponse } from '../types'
import './EnvVarsEditor.css'

interface Props {
  /** The row being edited — the id to save against, and the current values. */
  testEnv: TestEnvironmentResponse
  /** Confirmed environments are read-only; the list still shows. */
  readOnly: boolean
  /** Disable the Edit button while the page is busy with its own action. */
  pageBusy: boolean
  onSaved: (updated: TestEnvironmentResponse) => void
}

interface Row {
  key: string
  value: string
}

/**
 * The detected environment variables, viewable and editable in place.
 *
 * Its own component because it shares nothing with the rest of the
 * test-environment page except the row it edits: four state variables, two
 * handlers and ~90 lines of JSX that were 40% of the largest file in the
 * frontend, sitting inside a page otherwise concerned with a free-text
 * clarification loop.
 *
 * Editing here is uncapped and makes no LLM call — the extraction is
 * non-deterministic, and letting the user correct it directly is cheaper
 * than another round trip through the model.
 */
export default function EnvVarsEditor({ testEnv, readOnly, pageBusy, onSaved }: Props) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<Row[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const envVars = testEnv.env_vars
  if (!envVars) return null

  const startEditing = () => {
    setDraft(Object.entries(envVars).map(([key, value]) => ({ key, value })))
    setEditing(true)
    setError(null)
  }

  const updateRow = (index: number, patch: Partial<Row>) =>
    setDraft((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)))

  const handleSave = () => {
    // Blank rows are dropped rather than rejected: adding a row and then
    // changing your mind should not be an error state.
    const variables: Record<string, string> = {}
    for (const row of draft) {
      const key = row.key.trim()
      const value = row.value.trim()
      if (key && value) variables[key] = value
    }
    if (Object.keys(variables).length === 0) {
      setError('At least one variable is required.')
      return
    }
    setBusy(true)
    setError(null)
    updateTestEnvironmentVars(testEnv.id, variables)
      .then((updated) => {
        onSaved(updated)
        setEditing(false)
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setBusy(false))
  }

  return (
    <div className="test-env-vars">
      <h2>Detected environment variables</h2>
      {editing ? (
        <>
          {draft.map((row, index) => (
            <div key={index} className="test-env-vars-row">
              <input
                type="text"
                value={row.key}
                placeholder="NAME"
                aria-label={`Variable ${index + 1} name`}
                onChange={(e) => updateRow(index, { key: e.target.value })}
                disabled={busy}
              />
              <input
                type="text"
                value={row.value}
                placeholder="value"
                aria-label={`Variable ${index + 1} value`}
                onChange={(e) => updateRow(index, { value: e.target.value })}
                disabled={busy}
              />
              <button
                type="button"
                className="btn btn-danger btn-small"
                onClick={() => setDraft((prev) => prev.filter((_, i) => i !== index))}
                disabled={busy || draft.length === 1}
              >
                Remove
              </button>
            </div>
          ))}
          <div className="test-env-vars-actions">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setDraft((prev) => [...prev, { key: '', value: '' }])}
              disabled={busy}
            >
              Add variable
            </button>
            <button type="button" className="btn btn-primary" onClick={handleSave} disabled={busy}>
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setEditing(false)}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
          {error && <p className="test-env-error">{error}</p>}
        </>
      ) : (
        <>
          <ul className="test-env-vars-list">
            {Object.entries(envVars).map(([key, value]) => (
              <li key={key}>
                <code>{key}</code>: {value}
              </li>
            ))}
          </ul>
          {!readOnly && (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={startEditing}
              disabled={pageBusy}
            >
              Edit variables
            </button>
          )}
        </>
      )}
    </div>
  )
}
