import type { JobStatusResponse } from '../types'

interface WordCountProgressProps {
  jobStatus: JobStatusResponse
}

export default function WordCountProgress({ jobStatus }: WordCountProgressProps) {
  const pct =
    jobStatus.total_files > 0
      ? Math.round((jobStatus.processed_files / jobStatus.total_files) * 100)
      : 0

  if (jobStatus.status === 'queued' && jobStatus.processed_files === 0) {
    return (
      <div className="progress-waiting">
        <div className="waiting-pulse" />
        <p>Waiting for worker…</p>
      </div>
    )
  }

  return (
    <>
      <div className="progress-bar-bg">
        <div
          className="progress-bar-fill"
          style={{ width: `${pct}%` }}
          role="progressbar"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
        />
      </div>
      <p className="progress-text">
        Processing files: {jobStatus.processed_files} / {jobStatus.total_files} ({pct}%)
      </p>
    </>
  )
}
