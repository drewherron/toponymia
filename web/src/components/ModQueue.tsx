import { useEffect, useState } from 'react'
import { actOnReport, fetchReports } from '../api'
import { REPORT_CATEGORIES } from '../types'
import type { ReportAction, ReportCategory, ReportRow } from '../types'

function categoryLabel(value: ReportCategory): string {
  return REPORT_CATEGORIES.find((c) => c.value === value)?.label ?? value
}

interface ModQueueProps {
  onClose: () => void
}

function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function TargetSummary({ report }: { report: ReportRow }) {
  const target = report.target
  if (!target) {
    return <p className="mod-target-gone">The reported item no longer exists.</p>
  }
  return (
    <div className="mod-target">
      <p className="mod-target-head">
        {target.kind === 'talk_post' ? 'Talk post' : 'Revision'} by{' '}
        <strong>{target.author}</strong> on{' '}
        <a href={`/place/${target.slug}`}>{target.place}</a>
        {target.kind === 'talk_post' && <> — “{target.thread_title}”</>}
        {target.kind === 'revision' && target.is_current && (
          <span className="mod-current"> current</span>
        )}
      </p>
      {(target.kind === 'talk_post' && target.deleted) ||
      (target.kind === 'revision' && target.suppressed) ? (
        <p className="mod-target-gone">[already removed]</p>
      ) : (
        <blockquote className="mod-excerpt">{target.excerpt}</blockquote>
      )}
    </div>
  )
}

function ReportCard({
  report,
  onDone,
}: {
  report: ReportRow
  onDone: (id: number) => void
}) {
  const [busy, setBusy] = useState(false)
  const canDelete =
    report.target?.kind === 'talk_post' && !report.target.deleted
  const canSuppress =
    report.target?.kind === 'revision' &&
    !report.target.suppressed &&
    !report.target.is_current
  // A reported *current* revision has no take-down here — reverting it
  // (from the article's History tab) is the remedy.
  const revertHint =
    report.target?.kind === 'revision' &&
    report.target.is_current &&
    !report.target.suppressed

  const act = (action: ReportAction) => {
    setBusy(true)
    actOnReport(report.id, action)
      .then(() => onDone(report.id))
      .catch(console.error)
      .finally(() => setBusy(false))
  }

  return (
    <li className="mod-report">
      <TargetSummary report={report} />
      <p className="mod-report-meta">
        <span className="mod-report-category">
          {categoryLabel(report.category)}
        </span>{' '}
        · reported by <strong>{report.reporter}</strong> ·{' '}
        {formatWhen(report.created)}
        {report.reason && <> — “{report.reason}”</>}
      </p>
      {revertHint && (
        <p className="mod-revert-hint">
          This is the article’s current revision — revert it from the History
          tab to remove the content. Resolving here only closes the report.
        </p>
      )}
      <div className="mod-report-actions">
        {canDelete && (
          <button
            type="button"
            className="mod-action-delete"
            disabled={busy}
            onClick={() => act('delete')}
          >
            Remove content
          </button>
        )}
        {canSuppress && (
          <button
            type="button"
            className="mod-action-delete"
            disabled={busy}
            onClick={() => act('suppress')}
          >
            Suppress revision
          </button>
        )}
        <button type="button" disabled={busy} onClick={() => act('resolve')}>
          Resolve
        </button>
        <button type="button" disabled={busy} onClick={() => act('dismiss')}>
          Dismiss
        </button>
      </div>
    </li>
  )
}

function ModQueue({ onClose }: ModQueueProps) {
  const [reports, setReports] = useState<ReportRow[] | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    fetchReports(controller.signal)
      .then(setReports)
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          console.error(err)
          setError(true)
        }
      })
    return () => controller.abort()
  }, [])

  const handleDone = (id: number) =>
    setReports((prev) => (prev ? prev.filter((r) => r.id !== id) : prev))

  return (
    <div
      className="mod-queue-backdrop"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="mod-queue"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Moderator queue"
      >
        <div className="mod-queue-header">
          <h2>Reports</h2>
          <button
            type="button"
            className="mod-queue-close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </div>
        {error && (
          <p className="feature-pane-note">Could not load the queue.</p>
        )}
        {!error && reports === null && (
          <p className="feature-pane-note">Loading reports…</p>
        )}
        {reports !== null && reports.length === 0 && (
          <p className="feature-pane-note">No open reports. All clear.</p>
        )}
        {/* Every button here closes the report, so what separates them is
            invisible from the labels: whether the content changes, and what
            verdict goes on the reporter's record. Shown only alongside
            actual reports — with an empty queue it explains nothing. */}
        {reports !== null && reports.length > 0 && (
          <>
            <dl className="mod-queue-legend">
              <dt>Remove content / Suppress revision</dt>
              <dd>
                Takes it out of public view and closes the report. Reversible
                from Moderation → the author’s page.
              </dd>
              <dt>Resolve</dt>
              <dd>
                Closes the report as a fair one, leaving the content as it is —
                for when you’ve acted elsewhere (reverting a current revision)
                or nothing further is needed.
              </dd>
              <dt>Dismiss</dt>
              <dd>
                Closes the report as unfounded. Dismissals are counted per
                reporter under Moderation → Reporters, where a high tally is
                the signature of report-button abuse.
              </dd>
            </dl>
            <ul className="mod-report-list">
              {reports.map((report) => (
                <ReportCard
                  key={report.id}
                  report={report}
                  onDone={handleDone}
                />
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  )
}

export default ModQueue
