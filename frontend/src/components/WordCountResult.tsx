import type { JobStatusResponse } from '../types'
import FinishedResult from './FinishedResult'

interface WordCountResultProps {
  jobStatus: JobStatusResponse
}

export default function WordCountResult({ jobStatus }: WordCountResultProps) {
  switch (jobStatus.status) {
    case 'finished':
      return jobStatus.md_result && jobStatus.zip_results ? (
        <FinishedResult
          mdResult={jobStatus.md_result}
          zipResults={jobStatus.zip_results}
          totalWords={jobStatus.total_words}
        />
      ) : null

    case 'failed':
      return (
        <div className="job-error">
          <p className="job-error-title">Processing failed</p>
          <p className="job-error-detail">{jobStatus.error ?? 'Unknown error'}</p>
        </div>
      )

    case 'unknown':
      return (
        <div className="job-error">
          <p className="job-error-title">Job status unavailable</p>
          <p className="job-error-detail">
            The job result may have expired or the worker is not running.
          </p>
        </div>
      )

    default:
      return null
  }
}
