import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import WordCountProgress from './WordCountProgress'
import type { JobStatusResponse } from '../types'

function makeJob(overrides: Partial<JobStatusResponse> = {}): JobStatusResponse {
  return {
    job_id: 'test-job',
    status: 'started',
    total_files: 10,
    processed_files: 0,
    md_result: null,
    zip_results: null,
    total_words: null,
    error: null,
    ...overrides,
  }
}

describe('WordCountProgress', () => {
  it('shows waiting pulse when status is queued and nothing processed', () => {
    render(<WordCountProgress jobStatus={makeJob({ status: 'queued', processed_files: 0 })} />)
    expect(screen.getByText('Waiting for worker…')).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
  })

  it('shows progress bar when status is queued but some files already processed', () => {
    render(<WordCountProgress jobStatus={makeJob({ status: 'queued', processed_files: 3 })} />)
    expect(screen.getByRole('progressbar')).toBeInTheDocument()
    expect(screen.queryByText('Waiting for worker…')).not.toBeInTheDocument()
  })

  it('shows progress bar when status is started', () => {
    render(
      <WordCountProgress
        jobStatus={makeJob({ status: 'started', total_files: 5, processed_files: 2 })}
      />,
    )
    expect(screen.getByRole('progressbar')).toBeInTheDocument()
    expect(screen.queryByText('Waiting for worker…')).not.toBeInTheDocument()
  })

  it('computes 0%', () => {
    render(
      <WordCountProgress
        jobStatus={makeJob({ total_files: 10, processed_files: 0, status: 'started' })}
      />,
    )
    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '0')
    expect(bar.style.width).toBe('0%')
    expect(screen.getByText(/Processing files: 0 \/ 10 \(0%\)/)).toBeInTheDocument()
  })

  it('computes 50%', () => {
    render(
      <WordCountProgress
        jobStatus={makeJob({ total_files: 10, processed_files: 5, status: 'started' })}
      />,
    )

    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '50')
    expect(bar.style.width).toBe('50%')
    expect(screen.getByText(/Processing files: 5 \/ 10 \(50%\)/)).toBeInTheDocument()
  })

  it('computes 100%', () => {
    render(
      <WordCountProgress
        jobStatus={makeJob({ total_files: 3, processed_files: 3, status: 'finished' })}
      />,
    )
    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '100')
    expect(bar.style.width).toBe('100%')
    expect(screen.getByText(/Processing files: 3 \/ 3 \(100%\)/)).toBeInTheDocument()
  })

  it('sets aria-valuemin and aria-valuemax on progressbar', () => {
    render(<WordCountProgress jobStatus={makeJob()} />)
    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuemin', '0')
    expect(bar).toHaveAttribute('aria-valuemax', '100')
  })

  it('returns 0% when total_files is 0 (avoids division by zero)', () => {
    render(
      <WordCountProgress
        jobStatus={makeJob({ total_files: 0, processed_files: 0, status: 'started' })}
      />,
    )
    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '0')
    expect(screen.getByText(/Processing files: 0 \/ 0 \(0%\)/)).toBeInTheDocument()
  })

  it('rounds percentage to nearest integer', () => {
    // 1/3 ≈ 33.33% → 33%
    render(
      <WordCountProgress
        jobStatus={makeJob({ total_files: 3, processed_files: 1, status: 'started' })}
      />,
    )
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '33')
    expect(screen.getByText(/Processing files: 1 \/ 3 \(33%\)/)).toBeInTheDocument()
  })
})
