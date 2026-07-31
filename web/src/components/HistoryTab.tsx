import { useCallback, useEffect, useState } from 'react'
import { getRevision, listRevisions, revertArticle } from '../api'
import { diffBody, diffStructured } from '../diff'
import type { DiffSpan } from '../diff'
import type { ArticleData, RevisionDetail, RevisionSummary, User } from '../types'
import ArticleView from './ArticleView'
import ReportButton from './ReportButton'

interface HistoryTabProps {
  slug: string
  user: User | null
  /** Whether this user may revert (mirrors article protection). */
  canEdit: boolean
  onReverted: (article: ArticleData) => void
  /** The diff needs more room than the pane's default width. */
  onWideChange: (wide: boolean) => void
}

type Mode =
  | { type: 'list' }
  | { type: 'view'; revision: RevisionDetail }
  | { type: 'diff'; older: RevisionDetail; newer: RevisionDetail }

function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function asArticle(revision: RevisionDetail): ArticleData {
  return {
    content: revision.content,
    revision_id: revision.id,
    author: revision.author,
    created: revision.created,
    comment: revision.comment,
    protection_level: 'none',
  }
}

function Spans({ spans }: { spans: DiffSpan[] }) {
  return (
    <>
      {spans.map((span, i) => (
        <span key={i} className={`diff-${span.kind}`}>
          {span.text}
        </span>
      ))}
    </>
  )
}

function DiffView({
  older,
  newer,
}: {
  older: RevisionDetail
  newer: RevisionDetail
}) {
  const rows = diffBody(older.content.body_md, newer.content.body_md)
  const fieldChanges = diffStructured(older.content, newer.content)
  const bodyChanged = rows.some((row) => row.changed)
  // Bodies are legacy now — skip the panel when neither side has one.
  const hasBody =
    older.content.body_md.trim() !== '' ||
    newer.content.body_md.trim() !== ''
  return (
    <div className="diff">
      <div className="diff-side-headers">
        {[older, newer].map((revision) => (
          <div key={revision.id} className="diff-side-header">
            <strong>Revision {revision.id}</strong>
            {revision.is_current && (
              <span className="revision-current">current</span>
            )}
            <br />
            {revision.author} · {formatWhen(revision.created)}
            {revision.comment && <> — “{revision.comment}”</>}
          </div>
        ))}
      </div>

      {hasBody &&
        (bodyChanged ? (
          <div className="diff-rows">
            {rows.map((row, i) => (
              <div
                key={i}
                className={`diff-row${row.changed ? ' changed' : ''}`}
              >
                <div className={`diff-cell${row.left ? '' : ' absent'}`}>
                  {row.left && <Spans spans={row.left} />}
                </div>
                <div className={`diff-cell${row.right ? '' : ' absent'}`}>
                  {row.right && <Spans spans={row.right} />}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="feature-pane-note">Body text unchanged.</p>
        ))}

      {fieldChanges.length > 0 && (
        <div className="diff-fields">
          <h3>Names & structured fields</h3>
          {fieldChanges.map((change, i) => (
            <div key={i} className="diff-field">
              <span className={`diff-field-kind diff-field-${change.kind}`}>
                {change.kind}
              </span>
              <strong>{change.label}</strong>
              {change.kind === 'changed' && (
                <div className="diff-field-values">
                  <div className="diff-cell">
                    <span className="diff-del">{change.old}</span>
                  </div>
                  <div className="diff-cell">
                    <span className="diff-add">{change.new}</span>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function HistoryTab({
  slug,
  user,
  canEdit,
  onReverted,
  onWideChange,
}: HistoryTabProps) {
  const [revisions, setRevisions] = useState<RevisionSummary[] | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [error, setError] = useState(false)
  const [mode, setMode] = useState<Mode>({ type: 'list' })
  const [fromId, setFromId] = useState<number | null>(null)
  const [toId, setToId] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [reloads, setReloads] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setError(false)
    // A reload starts from the top: after a revert the newest page is the one
    // that changed, and any older pages the user had expanded are still older.
    listRevisions(slug, 0, controller.signal)
      .then((page) => {
        setRevisions(page.revisions)
        setHasMore(page.has_more)
        // sensible default: compare the latest change
        setToId(page.revisions[0]?.id ?? null)
        setFromId(page.revisions[1]?.id ?? null)
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          console.error(err)
          setError(true)
        }
      })
    return () => controller.abort()
  }, [slug, reloads])

  const loadOlder = useCallback(() => {
    if (revisions === null) return
    setBusy(true)
    listRevisions(slug, revisions.length)
      .then((page) => {
        setRevisions((current) => [...(current ?? []), ...page.revisions])
        setHasMore(page.has_more)
      })
      .catch(console.error)
      .finally(() => setBusy(false))
  }, [slug, revisions])

  useEffect(() => {
    onWideChange(mode.type === 'diff')
    return () => onWideChange(false)
  }, [mode.type, onWideChange])

  const compare = useCallback(() => {
    if (fromId === null || toId === null || fromId === toId) return
    setBusy(true)
    Promise.all([getRevision(slug, fromId), getRevision(slug, toId)])
      .then(([a, b]) => {
        const [older, newer] = a.id < b.id ? [a, b] : [b, a]
        setMode({ type: 'diff', older, newer })
      })
      .catch(console.error)
      .finally(() => setBusy(false))
  }, [slug, fromId, toId])

  const view = useCallback(
    (revisionId: number) => {
      setBusy(true)
      getRevision(slug, revisionId)
        .then((revision) => setMode({ type: 'view', revision }))
        .catch(console.error)
        .finally(() => setBusy(false))
    },
    [slug],
  )

  const revert = useCallback(
    (revisionId: number) => {
      setBusy(true)
      revertArticle(slug, revisionId)
        .then((article) => {
          onReverted(article)
          setMode({ type: 'list' })
          setReloads((n) => n + 1)
        })
        .catch(console.error)
        .finally(() => setBusy(false))
    },
    [slug, onReverted],
  )

  if (error) {
    return (
      <p className="feature-pane-note">Could not load the edit history.</p>
    )
  }
  if (revisions === null) {
    return <p className="feature-pane-note">Loading history…</p>
  }
  if (revisions.length === 0) {
    return (
      <p className="feature-pane-note">
        No revisions yet — this article hasn't been written.
      </p>
    )
  }

  if (mode.type === 'view') {
    const { revision } = mode
    return (
      <div className="history">
        <div className="revision-banner">
          <p>
            Viewing revision {revision.id} from{' '}
            {formatWhen(revision.created)} by <strong>{revision.author}</strong>
            {revision.is_current && <> (current)</>}
          </p>
          <div className="revision-banner-actions">
            <button type="button" onClick={() => setMode({ type: 'list' })}>
              ← Back to history
            </button>
            {canEdit && !revision.is_current && (
              <button
                type="button"
                className="revision-revert"
                disabled={busy}
                onClick={() => revert(revision.id)}
              >
                Revert to this revision
              </button>
            )}
            {(!user || user.username !== revision.author) && (
              <ReportButton
                targetType="revision"
                targetId={revision.id}
                loggedIn={!!user}
              />
            )}
          </div>
        </div>
        <ArticleView article={asArticle(revision)} />
      </div>
    )
  }

  if (mode.type === 'diff') {
    return (
      <div className="history">
        <button
          type="button"
          className="diff-back"
          onClick={() => setMode({ type: 'list' })}
        >
          ← Back to history
        </button>
        <DiffView older={mode.older} newer={mode.newer} />
      </div>
    )
  }

  return (
    <div className="history">
      {revisions.length > 1 && (
        <button
          type="button"
          className="history-compare"
          disabled={busy || fromId === null || toId === null || fromId === toId}
          onClick={compare}
        >
          Compare selected
        </button>
      )}
      <ol className="revision-list">
        {revisions.map((revision) => {
          // A suppressed revision still occupies its row — that row is the
          // attribution for text that may still be live — but for anyone but
          // a moderator it is inert: nothing to open, diff, revert or report,
          // because the server won't serve the snapshot behind it.
          const sealed = revision.suppressed && !user?.is_moderator
          return (
            <li
              key={revision.id}
              className={`revision-item${sealed ? ' revision-sealed' : ''}`}
            >
              {revisions.length > 1 && !sealed && (
                <span className="revision-radios">
                  <input
                    type="radio"
                    name="diff-from"
                    title="Older revision to compare"
                    checked={fromId === revision.id}
                    onChange={() => setFromId(revision.id)}
                  />
                  <input
                    type="radio"
                    name="diff-to"
                    title="Newer revision to compare"
                    checked={toId === revision.id}
                    onChange={() => setToId(revision.id)}
                  />
                </span>
              )}
              <div className="revision-meta">
                {sealed ? (
                  <span className="revision-when">
                    {formatWhen(revision.created)}
                  </span>
                ) : (
                  <button
                    type="button"
                    className="revision-view"
                    disabled={busy}
                    onClick={() => view(revision.id)}
                  >
                    {formatWhen(revision.created)}
                  </button>
                )}{' '}
                <strong>{revision.author}</strong>
                {revision.is_current && (
                  <span className="revision-current">current</span>
                )}
                {revision.suppressed && (
                  <span className="revision-removed">removed</span>
                )}
                {revision.comment && (
                  <span className="revision-comment">
                    “{revision.comment}”
                  </span>
                )}
                {canEdit && !revision.is_current && !sealed && (
                  <button
                    type="button"
                    className="revision-revert"
                    disabled={busy}
                    onClick={() => revert(revision.id)}
                  >
                    revert
                  </button>
                )}
                {!sealed && (!user || user.username !== revision.author) && (
                  <ReportButton
                    targetType="revision"
                    targetId={revision.id}
                    loggedIn={!!user}
                  />
                )}
              </div>
            </li>
          )
        })}
      </ol>
      {hasMore && (
        <button
          type="button"
          className="history-more"
          disabled={busy}
          onClick={loadOlder}
        >
          Load older revisions
        </button>
      )}
    </div>
  )
}

export default HistoryTab
