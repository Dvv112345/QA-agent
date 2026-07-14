import { useState } from 'react'
import { submitRequirements } from '../services/api'
import type { RequirementInput, RequirementResponse } from '../types'
import './RequirementForm.css'

interface Props {
  sprintId: number
  onSubmitted: (created: RequirementResponse[]) => void
}

const EMPTY_ROW: RequirementInput = { name: '', description: '' }

export default function RequirementForm({ sprintId, onSubmitted }: Props) {
  const [rows, setRows] = useState<RequirementInput[]>([{ ...EMPTY_ROW }])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const updateRow = (index: number, field: keyof RequirementInput, value: string) => {
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, [field]: value } : row)))
  }

  const addRow = () => setRows((prev) => [...prev, { ...EMPTY_ROW }])

  const removeRow = (index: number) => {
    setRows((prev) => (prev.length > 1 ? prev.filter((_, i) => i !== index) : prev))
  }

  const isRowBlank = (row: RequirementInput) => !row.name.trim() && !row.description.trim()
  const allBlank = rows.every(isRowBlank)

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const items = rows
      .filter((row) => !isRowBlank(row))
      .map((row) => ({ name: row.name.trim(), description: row.description.trim() }))
    if (items.length === 0) return

    setSubmitting(true)
    setError(null)
    submitRequirements(sprintId, items)
      .then((created) => {
        setRows([{ ...EMPTY_ROW }])
        onSubmitted(created)
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setSubmitting(false))
  }

  return (
    <form className="requirement-form" onSubmit={handleSubmit}>
      <h3>Add Requirements</h3>
      {rows.map((row, index) => (
        <div className="requirement-form-row" key={index}>
          <input
            type="text"
            value={row.name}
            onChange={(e) => updateRow(index, 'name', e.target.value)}
            placeholder="Requirement name"
            aria-label={`Requirement ${index + 1} name`}
            disabled={submitting}
          />
          <textarea
            value={row.description}
            onChange={(e) => updateRow(index, 'description', e.target.value)}
            placeholder="Describe the requirement…"
            aria-label={`Requirement ${index + 1} description`}
            rows={2}
            disabled={submitting}
          />
          <button
            type="button"
            className="btn btn-secondary requirement-form-remove"
            onClick={() => removeRow(index)}
            disabled={submitting || rows.length === 1}
            aria-label={`Remove row ${index + 1}`}
          >
            &times;
          </button>
        </div>
      ))}

      {error && <p className="requirement-form-error">{error}</p>}

      <div className="requirement-form-actions">
        <button type="button" className="btn btn-secondary" onClick={addRow} disabled={submitting}>
          + Add requirement
        </button>
        <button type="submit" className="btn btn-primary" disabled={submitting || allBlank}>
          {submitting ? 'Submitting…' : 'Submit Requirements'}
        </button>
      </div>
    </form>
  )
}
