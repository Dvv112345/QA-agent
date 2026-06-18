import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import WordCountResult from './WordCountResult'
import type { JobStatusResponse } from '../types'

function makeJob(overrides: Partial<JobStatusResponse> = {}): JobStatusResponse {
  return {
    job_id: 'test-job',
    status: 'finished',
    total_files: 1,
    processed_files: 1,
    md_result: { file: 'requirements.md', words: 42 },
    zip_results: [{ file: 'main.py', words: 100 }],
    total_words: 142,
    error: null,
    ...overrides,
  }
}

describe('WordCountResult', () => {
  it('renders FinishedResult when status is finished with results', () => {
    render(<WordCountResult jobStatus={makeJob()} />)
    expect(screen.getByText('Requirements Document')).toBeInTheDocument()
    expect(screen.getByText('requirements.md')).toBeInTheDocument()
    expect(screen.getByText('Source Files')).toBeInTheDocument()
    expect(screen.getByText('main.py')).toBeInTheDocument()
  })

  it('returns null when status is finished but md_result is null', () => {
    const { container } = render(
      <WordCountResult
        jobStatus={makeJob({
          status: 'finished',
          md_result: null,
          zip_results: [{ file: 'a.py', words: 1 }],
        })}
      />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('returns null when status is finished but zip_results is null', () => {
    const { container } = render(
      <WordCountResult
        jobStatus={makeJob({
          status: 'finished',
          md_result: { file: 'req.md', words: 5 },
          zip_results: null,
        })}
      />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('shows error message when status is failed', () => {
    render(
      <WordCountResult
        jobStatus={makeJob({
          status: 'failed',
          error: 'File missing: /tmp/broken.py',
          md_result: null,
          zip_results: null,
          total_words: null,
        })}
      />,
    )
    expect(screen.getByText('Processing failed')).toBeInTheDocument()
    expect(screen.getByText(/broken\.py/)).toBeInTheDocument()
  })

  it('shows fallback text when status is failed and error is null', () => {
    render(
      <WordCountResult
        jobStatus={makeJob({
          status: 'failed',
          error: null,
          md_result: null,
          zip_results: null,
          total_words: null,
        })}
      />,
    )
    expect(screen.getByText('Processing failed')).toBeInTheDocument()
    expect(screen.getByText('Unknown error')).toBeInTheDocument()
  })

  it('shows unavailable message when status is unknown', () => {
    render(
      <WordCountResult
        jobStatus={makeJob({
          status: 'unknown',
          md_result: null,
          zip_results: null,
          total_words: null,
        })}
      />,
    )
    expect(screen.getByText('Job status unavailable')).toBeInTheDocument()
  })

  it('returns null for queued status', () => {
    const { container } = render(<WordCountResult jobStatus={makeJob({ status: 'queued' })} />)
    expect(container.firstChild).toBeNull()
  })

  it('returns null for started status', () => {
    const { container } = render(<WordCountResult jobStatus={makeJob({ status: 'started' })} />)
    expect(container.firstChild).toBeNull()
  })
})
