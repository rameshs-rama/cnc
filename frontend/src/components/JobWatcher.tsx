import { useEffect, useState } from 'react'
import { request, type Job } from '@/lib/api'
import { ErrorNote, ProgressBar } from './ui'

/**
 * Follows an asynchronous engineering job to completion.
 *
 * Long-running jobs must show stage, percent and diagnostics, and must be
 * cancellable (PRD 6.1).
 */
export function JobWatcher({
  jobId,
  onDone,
  onFailed,
}: {
  jobId: string
  onDone: (job: Job) => void
  onFailed?: (job: Job) => void
}) {
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    let active = true
    let timer: number

    const poll = async () => {
      try {
        const next = await request<Job>(`/jobs/${jobId}`)
        if (!active) return
        setJob(next)
        if (next.status === 'Succeeded') return onDone(next)
        if (next.status === 'Failed' || next.status === 'Cancelled') return onFailed?.(next)
        timer = window.setTimeout(poll, 700)
      } catch (caught) {
        if (active) setError(caught)
      }
    }
    void poll()

    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [jobId, onDone, onFailed])

  if (error) return <ErrorNote error={error} />
  if (!job) return <ProgressBar percent={2} label="queued" />

  return (
    <div className="job-watcher">
      <ProgressBar percent={job.percent} label={`${job.stage} — ${job.percent.toFixed(0)}%`} />
      <div className="job-meta">
        <span>{job.kind}</span>
        <span className="muted">attempt {job.attempts}</span>
        <span className="muted">trace {job.trace_id}</span>
        {job.status === 'Running' || job.status === 'Queued' ? (
          <button
            type="button"
            onClick={() => {
              void request(`/jobs/${job.id}:cancel`, { method: 'POST' })
            }}
          >
            Cancel
          </button>
        ) : null}
      </div>
      {job.error ? <p className="error-text">{job.error}</p> : null}
    </div>
  )
}
