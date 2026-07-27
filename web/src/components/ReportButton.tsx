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
  label = 'report',
}: {
  targetType: 'revision' | 'talk_post'
  targetId: number
  /** Logged-out users see the same link, disabled, with a login tooltip. */
  loggedIn?: boolean
  label?: string
}) {
  const [open, setOpen] = useState(false)
  const [category, setCategory] = useState<ReportCategory>('other')
  const [reason, setReason] = useState('')
  const [state, setState] = useState<'idle' | 'busy' | 'done'>('idle')

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

  if (state === 'done') {
    return <span className="report-done">reported</span>
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
        setState('idle')
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
    </form>
  )
}

export default ReportButton
