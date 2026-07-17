import { useState } from 'react'
import type { FormEvent } from 'react'
import { createReport } from '../api'

/** Inline "report" affordance: reveals a reason box and files a flag on
 * a revision or a talk post for moderator attention (DESIGN.md §6). */
function ReportButton({
  targetType,
  targetId,
  label = 'report',
}: {
  targetType: 'revision' | 'talk_post'
  targetId: number
  label?: string
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [state, setState] = useState<'idle' | 'busy' | 'done'>('idle')

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
    createReport(targetType, targetId, reason.trim())
      .then(() => setState('done'))
      .catch((error) => {
        console.error(error)
        setState('idle')
      })
  }
  return (
    <form className="report-form" onSubmit={submit}>
      <input
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        maxLength={500}
        placeholder="Why report this? (optional)"
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
