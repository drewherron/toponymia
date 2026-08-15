import { useState } from 'react'
import type { FormEvent } from 'react'
import { createReport } from '../api'
import { REPORT_CATEGORIES } from '../types'
import type { ReportCategory } from '../types'

/** Inline "report" affordance: reveals a reason box and files a flag on
 * a revision or a talk post for moderator attention. */
function ReportButton({
  targetType,
  targetId,
  loggedIn = true,
  reported = false,
  label = 'report',
}: {
  targetType: 'revision' | 'talk_post'
  targetId: number
  /** Logged-out users see the same link, disabled, with a login tooltip. */
  loggedIn?: boolean
  /** The server's answer to "have I reported this before?". Read on every
   *  render rather than seeded into state: a refetch that returns a fresh
   *  post object must not walk the marker back to a live button. */
  reported?: boolean
  label?: string
}) {
  const [open, setOpen] = useState(false)
  const [category, setCategory] = useState<ReportCategory>('other')
  const [reason, setReason] = useState('')
  const [state, setState] = useState<'idle' | 'busy' | 'failed' | 'done'>(
    'idle',
  )

  if (!loggedIn) {
    return (
      <button
        type="button"
        className="report-button report-button-disabled"
        aria-disabled="true"
        title="Log in to report"
        onClick={(e) => e.preventDefault()}
      >
        {label}
      </button>
    )
  }

  // `reported` first: a report filed in an earlier session counts the same as
  // one filed a second ago, and the server is the one that knows.
  if (reported || state === 'done') {
    return (
      <span className="report-done" title="You reported this">
        reported
      </span>
    )
  }
  if (!open) {
    return (
      <button
        type="button"
        className="report-button"
        onClick={() => setOpen(true)}
      >
        {label}
      </button>
    )
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    setState('busy')
    createReport(targetType, targetId, category, reason.trim())
      .then(() => setState('done'))
      .catch((error) => {
        console.error(error)
        // Leaves the form open and says so. Dropping back to a bare "report"
        // button was indistinguishable from never having clicked, so a failed
        // report looked exactly like a successful one that didn't stick.
        setState('failed')
      })
  }
  return (
    <form className="report-form" onSubmit={submit}>
      <select
        className="report-category"
        value={category}
        onChange={(e) => setCategory(e.target.value as ReportCategory)}
        aria-label="Report reason"
      >
        {REPORT_CATEGORIES.map((c) => (
          <option key={c.value} value={c.value}>
            {c.label}
          </option>
        ))}
      </select>
      <input
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        maxLength={500}
        placeholder="Add detail (optional)"
        autoFocus
      />
      <button type="submit" disabled={state === 'busy'}>
        Send
      </button>
      <button type="button" onClick={() => setOpen(false)}>
        Cancel
      </button>
      {state === 'failed' && (
        <span className="report-error" role="alert">
          That didn’t send. Try again in a moment.
        </span>
      )}
    </form>
  )
}

export default ReportButton
