import { useCallback, useEffect, useId, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import {
  banUser,
  fetchModAudit,
  fetchModReporters,
  fetchModUser,
  fetchModUsers,
  restoreRevision,
  restoreTalkPost,
  restoreTalkThread,
  setUserRole,
  unbanUser,
} from '../api'
import { REPORT_CATEGORIES } from '../types'
import type {
  ModAuditPage,
  ModReporter,
  ModUserDetail,
  ModUserRow,
  RemovedContent,
  User,
  UserRole,
} from '../types'

function when(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function categoryLabel(value: string): string {
  return REPORT_CATEGORIES.find((c) => c.value === value)?.label ?? value
}

function RoleBadge({ role }: { role: UserRole }) {
  if (role === 'user') return null
  return <span className={`mod-role mod-role-${role}`}>{role}</span>
}

// --- ban controls ---------------------------------------------------

const EXPIRY_OPTIONS = [
  { days: 0, label: 'Permanent' },
  { days: 1, label: '1 day' },
  { days: 7, label: '7 days' },
  { days: 30, label: '30 days' },
]

/** What a ban does, spelled out at the moment of deciding.
 *
 *  The first line is the one worth reprinting every time: a ban is a write
 *  block, not a login block, and assuming otherwise is the natural mistake —
 *  it is the difference between "they're gone" and "they can still read every
 *  page and see what you wrote about them". */
function BanEffects({ username }: { username: string }) {
  return (
    <ul className="mod-ban-effects">
      <li>
        <strong>{username} can still sign in and read.</strong> A ban blocks
        writing: edits, talk posts, reports and reverts are all refused, with
        the reason below shown to them.
      </li>
      <li>
        Their email address can’t open a new account for as long as the ban
        lasts.
      </li>
      <li>
        Everything they’ve written <strong>stays up</strong>, under their name,
        unless you also remove it.
      </li>
      <li>
        Lifting the ban restores writing and unblocks the address — but it does
        not restore anything removed here.
      </li>
    </ul>
  )
}

function BanForm({
  user,
  isAdmin,
  onDone,
}: {
  user: ModUserDetail
  isAdmin: boolean
  onDone: (removed: RemovedContent | null) => void
}) {
  const [reason, setReason] = useState('')
  const [expiryDays, setExpiryDays] = useState(0)
  const [removeContent, setRemoveContent] = useState(false)
  const [busy, setBusy] = useState(false)

  const submit = () => {
    // Removal deletes articles and can't be undone in one step, so it asks —
    // the plain ban, which is reversible and touches nothing, does not.
    if (
      removeContent &&
      !window.confirm(
        `Remove everything ${user.username} has written?\n\n` +
          'Talk posts and revisions are hidden from the public, articles they ' +
          'last edited are reverted, and articles only they have written are ' +
          'taken down. Undoing it means restoring each item by hand.',
      )
    ) {
      return
    }
    setBusy(true)
    banUser(user.id, {
      reason: reason.trim(),
      expires_days: expiryDays,
      remove_content: removeContent,
    })
      .then(onDone)
      .catch(console.error)
      .finally(() => setBusy(false))
  }

  return (
    <div className="mod-ban-form">
      <h4>Ban {user.username}</h4>
      <BanEffects username={user.username} />
      <input
        className="mod-ban-reason"
        value={reason}
        maxLength={500}
        placeholder="Reason (shown to the user)"
        onChange={(e) => setReason(e.target.value)}
      />
      <label className="mod-ban-field">
        Duration
        <select
          value={expiryDays}
          onChange={(e) => setExpiryDays(Number(e.target.value))}
        >
          {EXPIRY_OPTIONS.map((o) => (
            <option key={o.days} value={o.days}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      {/* Admin-only: removal can take down whole articles, which matches the
          grant on article deletion itself. The server enforces this too. */}
      {isAdmin && (
        <label className="mod-ban-check">
          <input
            type="checkbox"
            checked={removeContent}
            onChange={(e) => setRemoveContent(e.target.checked)}
          />
          <span>
            Also remove all their content
            <small className="mod-ban-check-note">
              Hides every talk post and revision of theirs from the public,
              reverts articles they last edited, and deletes articles only
              they have written. Reversible, item by item — unbanning does
              not undo it.
            </small>
          </span>
        </label>
      )}
      <button
        type="button"
        className="mod-action-delete"
        disabled={busy}
        onClick={submit}
      >
        Ban account
      </button>
    </div>
  )
}

/** The receipt for a removal: what it actually took down.
 *
 *  Worth showing because the counts are not guessable from the outside — the
 *  article lines especially, since whether a given article was reverted or
 *  deleted depends on whether anyone else had ever edited it. */
function RemovalSummary({ removed }: { removed: RemovedContent }) {
  const lines: string[] = []
  const plural = (n: number, one: string, many: string) =>
    `${n} ${n === 1 ? one : many}`
  if (removed.talk_posts > 0) {
    lines.push(plural(removed.talk_posts, 'talk post', 'talk posts') + ' hidden')
  }
  if (removed.revisions > 0) {
    lines.push(
      plural(removed.revisions, 'revision', 'revisions') +
        ' hidden (the byline stays, for attribution)',
    )
  }
  if (removed.articles_reverted > 0) {
    lines.push(
      plural(removed.articles_reverted, 'article', 'articles') +
        ' reverted to the last edit by someone else',
    )
  }
  if (removed.articles_deleted > 0) {
    lines.push(
      plural(removed.articles_deleted, 'article', 'articles') +
        ' taken down — nobody else had written ' +
        (removed.articles_deleted === 1 ? 'it' : 'them'),
    )
  }

  return (
    <div className="mod-removal-summary">
      <p>
        <strong>Content removed.</strong>
        {lines.length === 0 && ' They had nothing published.'}
      </p>
      {lines.length > 0 && (
        <ul>
          {lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
      {lines.length > 0 && (
        <p className="mod-note">
          Nothing is erased — moderators still see all of it, and each item can
          be restored individually below.
        </p>
      )}
    </div>
  )
}

function BanPanel({
  user,
  isAdmin,
  removed,
  onChanged,
}: {
  user: ModUserDetail
  isAdmin: boolean
  removed: RemovedContent | null
  onChanged: (removed: RemovedContent | null) => void
}) {
  const [busy, setBusy] = useState(false)
  const activeBan = user.bans.find((b) => b.active)

  const unban = () => {
    setBusy(true)
    unbanUser(user.id)
      // A lift doesn't restore content, so the removal receipt would be
      // stale — and misleading — next to an unbanned account.
      .then(() => onChanged(null))
      .catch(console.error)
      .finally(() => setBusy(false))
  }

  if (activeBan) {
    return (
      <div className="mod-ban-panel mod-ban-active">
        <p>
          <strong>Suspended</strong>
          {activeBan.expires
            ? ` until ${when(activeBan.expires)}`
            : ' permanently'}
          {activeBan.reason && <> — “{activeBan.reason}”</>}
          {activeBan.created_by && <> (by {activeBan.created_by})</>}
        </p>
        <p className="mod-note">
          They can still sign in and read; writing is blocked until this is
          lifted.
        </p>
        {removed && <RemovalSummary removed={removed} />}
        {user.can_ban ? (
          <button type="button" disabled={busy} onClick={unban}>
            Lift ban
          </button>
        ) : (
          <p className="mod-note">
            You don’t have authority over this account.
          </p>
        )}
      </div>
    )
  }
  if (!user.can_ban) {
    return (
      <p className="mod-note">
        This account can’t be banned by you (moderators can only be banned
        by an admin; admins can’t be banned).
      </p>
    )
  }
  return <BanForm user={user} isAdmin={isAdmin} onDone={onChanged} />
}

// --- role controls --------------------------------------------------

// Promote/demote is admin-only and deliberately confirmed: it is the one
// control here that changes what an account *is* rather than what it may do.
function RolePanel({
  user,
  onChanged,
}: {
  user: ModUserDetail
  onChanged: () => void
}) {
  const [busy, setBusy] = useState(false)
  if (!user.can_set_role) return null

  const promote = user.role === 'user'
  const submit = () => {
    const verb = promote ? 'Promote' : 'Demote'
    const detail = promote
      ? `${user.username} will be able to handle reports, remove content, and ban accounts.`
      : `${user.username} will lose all moderator powers.`
    if (!window.confirm(`${verb} ${user.username}? ${detail}`)) return
    setBusy(true)
    setUserRole(user.id, promote ? 'moderator' : 'user')
      .then(onChanged)
      .catch(console.error)
      .finally(() => setBusy(false))
  }

  return (
    <div className="mod-role-panel">
      <p className="mod-note">
        {promote
          ? 'A moderator can handle reports, remove content, and ban regular users.'
          : 'Demoting returns this account to a regular user.'}
      </p>
      <button
        type="button"
        className="mod-role-button"
        disabled={busy}
        onClick={submit}
      >
        {promote ? 'Promote to moderator' : 'Demote to user'}
      </button>
    </div>
  )
}

/** The inverse of a removal, next to the removed thing itself. M12 built
 *  both restore endpoints and then never gave either one a button, so until
 *  now a removal could only be undone from a shell. */
function RestoreButton({
  onRestore,
  onDone,
}: {
  onRestore: () => Promise<void>
  onDone: () => void
}) {
  const [busy, setBusy] = useState(false)
  return (
    <button
      type="button"
      className="mod-restore-button"
      disabled={busy}
      onClick={() => {
        setBusy(true)
        onRestore()
          .then(onDone)
          .catch(console.error)
          .finally(() => setBusy(false))
      }}
    >
      Restore
    </button>
  )
}

// --- user detail ----------------------------------------------------

/** One collapsible list in the user panel, matching the Talk tab's threads.
 *
 *  Collapsed to start: a busy account carries a hundred posts and a hundred
 *  revisions, and the panel's job on open is to let a moderator see the
 *  shape of the account — five counts — before choosing what to read. */
function DetailSection({
  title,
  count,
  children,
}: {
  title: string
  count: number
  children: ReactNode
}) {
  const bodyId = useId()
  const [collapsed, setCollapsed] = useState(true)
  return (
    <section className="mod-detail-section">
      <h4>
        <button
          type="button"
          className="mod-section-toggle"
          aria-expanded={!collapsed}
          aria-controls={bodyId}
          onClick={() => setCollapsed((value) => !value)}
        >
          <span className="mod-section-caret" aria-hidden="true">
            {collapsed ? '▸' : '▾'}
          </span>
          <span className="mod-section-title">{title}</span>
          <span className="mod-section-count">{count}</span>
        </button>
      </h4>
      {/* Unmounted rather than hidden, like a collapsed thread: these lists
          carry the full body of every post and revision excerpt, which is
          exactly the weight the collapse exists to avoid. */}
      <div id={bodyId} hidden={collapsed}>
        {!collapsed && children}
      </div>
    </section>
  )
}

function UserDetail({
  userId,
  isAdmin,
  onChanged,
}: {
  userId: number
  isAdmin: boolean
  onChanged: () => void
}) {
  const [detail, setDetail] = useState<ModUserDetail | null>(null)
  const [error, setError] = useState(false)
  // Lives here, not in BanPanel: the reload below unmounts every child while
  // it runs, and the receipt has to outlast that to be readable at all. Reset
  // on a change of user so it can't be read as the new one's.
  const [removed, setRemoved] = useState<RemovedContent | null>(null)

  const load = useCallback(() => {
    setDetail(null)
    setError(false)
    fetchModUser(userId)
      .then(setDetail)
      .catch(() => setError(true))
  }, [userId])

  useEffect(() => {
    setRemoved(null)
  }, [userId])

  useEffect(load, [load])

  if (error) return <p className="mod-note">Could not load this user.</p>
  if (!detail) return <p className="mod-note">Loading…</p>

  const refresh = () => {
    load()
    onChanged()
  }

  const afterBan = (result: RemovedContent | null) => {
    setRemoved(result)
    refresh()
  }

  return (
    <div className="mod-user-detail">
      <h3>
        {detail.username}
        <RoleBadge role={detail.role} />
      </h3>
      <p className="mod-note">Joined {when(detail.date_joined)}</p>

      <BanPanel
        user={detail}
        isAdmin={isAdmin}
        removed={removed}
        onChanged={afterBan}
      />
      <RolePanel user={detail} onChanged={refresh} />

      <DetailSection
        title="Reports against them"
        count={detail.reports_against.length}
      >
        {detail.reports_against.length === 0 ? (
          <p className="mod-note">None.</p>
        ) : (
          <ul className="mod-detail-list">
            {detail.reports_against.map((r) => (
              <li key={r.id}>
                <span className="mod-report-category">
                  {categoryLabel(r.category)}
                </span>{' '}
                <span className={`mod-status mod-status-${r.status}`}>
                  {r.status}
                </span>{' '}
                on a {r.target_kind === 'talk_post' ? 'talk post' : 'revision'}{' '}
                by {r.reporter} · {when(r.created)}
                {r.reason && <> — “{r.reason}”</>}
              </li>
            ))}
          </ul>
        )}
      </DetailSection>

      <DetailSection title="Talk posts" count={detail.talk_posts.length}>
        <ul className="mod-detail-list">
          {detail.talk_posts.map((p) => (
            <li key={p.id} className={p.deleted ? 'mod-removed' : ''}>
              {p.deleted && (
                <>
                  <span className="mod-removed-tag">removed</span>{' '}
                  <RestoreButton
                    onRestore={() => restoreTalkPost(p.id)}
                    onDone={refresh}
                  />{' '}
                </>
              )}
              <a href={`/place/${p.slug}`}>{p.place}</a> — “{p.thread_title}”
              <blockquote>{p.body_md}</blockquote>
            </li>
          ))}
        </ul>
      </DetailSection>

      <DetailSection
        title="Threads started"
        count={detail.talk_threads.length}
      >
        {detail.talk_threads.length === 0 ? (
          <p className="mod-note">None.</p>
        ) : (
          <ul className="mod-detail-list">
            {detail.talk_threads.map((t) => (
              <li key={t.id} className={t.deleted ? 'mod-removed' : ''}>
                {t.deleted && (
                  <>
                    <span className="mod-removed-tag">removed</span>{' '}
                    <RestoreButton
                      onRestore={() => restoreTalkThread(t.id)}
                      onDone={refresh}
                    />{' '}
                  </>
                )}
                <a href={`/place/${t.slug}`}>{t.place}</a> — “{t.title}”{' '}
                <span className="mod-note">
                  ({t.post_count === 1 ? '1 post' : `${t.post_count} posts`})
                </span>
              </li>
            ))}
          </ul>
        )}
      </DetailSection>

      <DetailSection title="Revisions" count={detail.revisions.length}>
        <ul className="mod-detail-list">
          {detail.revisions.map((r) => (
            <li key={r.id} className={r.suppressed ? 'mod-removed' : ''}>
              {r.suppressed && (
                <>
                  <span className="mod-removed-tag">suppressed</span>{' '}
                  <RestoreButton
                    onRestore={() => restoreRevision(r.id)}
                    onDone={refresh}
                  />{' '}
                </>
              )}
              {r.is_current && <span className="mod-current">current</span>}{' '}
              <a href={`/place/${r.slug}`}>{r.place}</a>
              {r.excerpt && <blockquote>{r.excerpt}</blockquote>}
            </li>
          ))}
        </ul>
      </DetailSection>

      <DetailSection title="Actions taken" count={detail.audit.length}>
        {detail.audit.length === 0 ? (
          <p className="mod-note">None.</p>
        ) : (
          <ul className="mod-detail-list">
            {detail.audit.map((a) => (
              <li key={a.id}>
                <code>{a.action}</code> by {a.actor ?? '—'} · {when(a.created)}
                {a.reason && <> — “{a.reason}”</>}
              </li>
            ))}
          </ul>
        )}
      </DetailSection>
    </div>
  )
}

// --- users tab ------------------------------------------------------

type UserSort = 'recent' | 'username' | 'open' | 'reports' | 'removed'

// Kept terse on purpose: a native select is as wide as its longest option,
// and this one shares a 340px column with the "Show all" checkbox.
const USER_SORTS: { key: UserSort; label: string }[] = [
  { key: 'recent', label: 'Recent' },
  { key: 'username', label: 'Name' },
  { key: 'open', label: 'Open' },
  { key: 'reports', label: 'Reports' },
  { key: 'removed', label: 'Removed' },
]

/** Order the list by one column, alphabetically within ties — so a sort by
 *  a count that most rows share still reads as a stable list rather than
 *  whatever order the server happened to send. */
function sortUsers(rows: ModUserRow[], sort: UserSort): ModUserRow[] {
  const byName = (a: ModUserRow, b: ModUserRow) =>
    a.username.toLowerCase().localeCompare(b.username.toLowerCase())
  return [...rows].sort((a, b) => {
    switch (sort) {
      case 'username':
        return byName(a, b)
      case 'open':
        return b.reports_open - a.reports_open || byName(a, b)
      case 'reports':
        return b.reports_total - a.reports_total || byName(a, b)
      case 'removed':
        return b.removed_count - a.removed_count || byName(a, b)
      default:
        // The server's own order: most recently reported first, with the
        // never-reported (null timestamp) falling to the bottom.
        return (
          (b.last_report ?? '').localeCompare(a.last_report ?? '') ||
          byName(a, b)
        )
    }
  })
}

function UsersTab({ isAdmin }: { isAdmin: boolean }) {
  const [rows, setRows] = useState<ModUserRow[] | null>(null)
  const [truncated, setTruncated] = useState(false)
  const [filter, setFilter] = useState('')
  const [selected, setSelected] = useState<number | null>(null)
  // Defaults to what the server already sends, so opening the tab looks
  // exactly as it did before the control existed.
  const [sort, setSort] = useState<UserSort>('recent')
  // The default list is moderation-shaped: only accounts with something
  // against them. An admin promoting a well-behaved contributor needs the
  // whole roster, which nothing else here would ever surface.
  const [showAll, setShowAll] = useState(false)

  const load = useCallback(() => {
    fetchModUsers({ all: showAll })
      .then((result) => {
        setRows(result.users)
        setTruncated(result.truncated)
      })
      .catch(console.error)
  }, [showAll])

  useEffect(load, [load])

  const shown = useMemo(() => {
    if (!rows) return []
    const q = filter.trim().toLowerCase()
    const matched = q
      ? rows.filter((r) => r.username.toLowerCase().includes(q))
      : rows
    return sortUsers(matched, sort)
  }, [rows, filter, sort])

  return (
    <div className="mod-users">
      <div className="mod-users-list">
        <input
          className="mod-filter"
          value={filter}
          placeholder="Filter users…"
          onChange={(e) => setFilter(e.target.value)}
        />
        <div className="mod-list-controls">
          {isAdmin && (
            <label className="mod-show-all">
              <input
                type="checkbox"
                checked={showAll}
                onChange={(e) => setShowAll(e.target.checked)}
              />
              Show all
            </label>
          )}
          {/* Not `mod-sort` — that class belongs to the Reporters table's
              sortable column headers, whose rules sit later in the
              stylesheet and would win. */}
          <label className="mod-user-sort">
            Sort
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as UserSort)}
            >
              {USER_SORTS.map((option) => (
                <option key={option.key} value={option.key}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        {truncated && (
          <p className="mod-note mod-truncated">
            Showing the first {rows?.length ?? 0} accounts — filtering searches
            only these.
          </p>
        )}
        {rows === null && <p className="mod-note">Loading…</p>}
        {rows !== null && shown.length === 0 && (
          <p className="mod-note">No users to show.</p>
        )}
        <ul>
          {shown.map((r) => (
            <li key={r.id}>
              <button
                type="button"
                className={`mod-user-row${
                  selected === r.id ? ' active' : ''
                }`}
                onClick={() => setSelected(r.id)}
              >
                <span className="mod-user-name">
                  {r.username}
                  <RoleBadge role={r.role} />
                  {r.banned && <span className="mod-banned-tag">banned</span>}
                </span>
                <span className="mod-user-stats">
                  {r.reports_open > 0 && (
                    <span className="mod-stat-open">{r.reports_open} open</span>
                  )}
                  <span>{r.reports_total} reports</span>
                  {r.removed_count > 0 && (
                    <span>{r.removed_count} removed</span>
                  )}
                </span>
                <span className="mod-user-when">{when(r.last_report)}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div className="mod-users-detail">
        {selected === null ? (
          <p className="mod-note">Select a user to review.</p>
        ) : (
          <UserDetail userId={selected} isAdmin={isAdmin} onChanged={load} />
        )}
      </div>
    </div>
  )
}

// --- reporters tab --------------------------------------------------

// Share of *decided* reports that were dismissed. Open reports are excluded:
// they haven't been judged, so counting them would let a backlog flatter a
// reporter. Null when nothing has been decided yet.
function dismissedRate(r: ModReporter): number | null {
  const decided = r.resolved + r.dismissed
  return decided === 0 ? null : r.dismissed / decided
}

type ReporterSort = 'username' | 'total' | 'open' | 'resolved' | 'dismissed' | 'rate'

const REPORTER_COLUMNS: { key: ReporterSort; label: string }[] = [
  { key: 'username', label: 'Reporter' },
  { key: 'total', label: 'Total' },
  { key: 'open', label: 'Open' },
  { key: 'resolved', label: 'Upheld' },
  { key: 'dismissed', label: 'Dismissed' },
  { key: 'rate', label: 'Dismissed rate' },
]

// Names read best A–Z; every count reads best biggest-first.
function defaultDir(key: ReporterSort): 'asc' | 'desc' {
  return key === 'username' ? 'asc' : 'desc'
}

function ReportersTab() {
  const [rows, setRows] = useState<ModReporter[] | null>(null)
  // Matches the server's own order (-dismissed, -total): the abuse-finding
  // sort, and the default for a reason — ranking by volume would put the most
  // prolific (usually most helpful) reporter on top.
  const [sort, setSort] = useState<ReporterSort>('dismissed')
  const [dir, setDir] = useState<'asc' | 'desc'>('desc')

  useEffect(() => {
    fetchModReporters().then(setRows).catch(console.error)
  }, [])

  const sorted = useMemo(() => {
    if (rows === null) return null
    const sign = dir === 'asc' ? 1 : -1
    return [...rows].sort((a, b) => {
      if (sort === 'username') return sign * a.username.localeCompare(b.username)
      if (sort === 'rate') {
        // A reporter with nothing decided has no rate — park them at the
        // bottom either way rather than letting null rank as zero.
        const ra = dismissedRate(a)
        const rb = dismissedRate(b)
        if (ra === null || rb === null) {
          if (ra === rb) return b.total - a.total
          return ra === null ? 1 : -1
        }
        return sign * (ra - rb) || b.total - a.total
      }
      return sign * (a[sort] - b[sort]) || b.total - a.total
    })
  }, [rows, sort, dir])

  function sortBy(key: ReporterSort) {
    if (key === sort) setDir(dir === 'asc' ? 'desc' : 'asc')
    else {
      setSort(key)
      setDir(defaultDir(key))
    }
  }

  if (sorted === null) return <p className="mod-note">Loading…</p>
  if (sorted.length === 0) return <p className="mod-note">No reports yet.</p>

  return (
    <div className="mod-reporters">
      <p className="mod-note">
        Dismissed rate is the share of a reporter's decided reports that were
        dismissed; open reports aren't counted. A high rate can signal report
        abuse — but read it alongside the number of decisions it's based on,
        since one dismissed report reads as 100%.
      </p>
      <table className="mod-table">
        <thead>
          <tr>
            {REPORTER_COLUMNS.map((col) => (
              <th
                key={col.key}
                aria-sort={
                  sort === col.key
                    ? dir === 'asc'
                      ? 'ascending'
                      : 'descending'
                    : 'none'
                }
              >
                <button
                  type="button"
                  className="mod-sort"
                  onClick={() => sortBy(col.key)}
                >
                  {col.label}
                  {sort === col.key && (
                    <span aria-hidden="true" className="mod-sort-arrow">
                      {dir === 'asc' ? '▲' : '▼'}
                    </span>
                  )}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => {
            const rate = dismissedRate(r)
            return (
              <tr key={r.id} className={r.dismissed > r.resolved ? 'mod-flag' : ''}>
                <td>{r.username}</td>
                <td>{r.total}</td>
                <td>{r.open}</td>
                <td>{r.resolved}</td>
                <td>{r.dismissed}</td>
                <td className="mod-rate">
                  {rate === null ? (
                    <span className="mod-rate-none" title="No reports decided yet">
                      —
                    </span>
                  ) : (
                    <>
                      {Math.round(rate * 100)}%
                      <span className="mod-rate-basis">
                        {' '}
                        of {r.resolved + r.dismissed}
                      </span>
                    </>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// --- audit tab ------------------------------------------------------

/** Human wording for each ModAction kind. The raw enum leaks in the
 *  per-user trail; a feed meant for scanning deserves better. */
const ACTION_LABEL: Record<string, string> = {
  delete_post: 'removed a talk post',
  restore_post: 'restored a talk post',
  suppress_revision: 'suppressed a revision',
  restore_revision: 'restored a revision',
  delete_thread: 'removed a talk thread',
  restore_thread: 'restored a talk thread',
  delete_article: 'deleted an article',
  restore_article: 'restored an article',
  revert_article: 'reverted an article',
  ban_user: 'banned',
  unban_user: 'unbanned',
  promote_mod: 'promoted to moderator',
  demote_mod: 'demoted to user',
  resolve_report: 'resolved a report',
  dismiss_report: 'dismissed a report',
}

/** The whole point of the feed: removals stand out when you scan it. */
/** The whole point of the feed: what a row *did* should be legible before
 *  you read it. Four tones on one axis, worst to best.
 *
 *  `severe` takes something (or someone) out of public view; `corrective`
 *  changes or narrows without hiding — a revert is undoable and leaves the
 *  history intact, which is why it sits below a removal rather than beside
 *  one; `judgement` closes a report and touches no content at all;
 *  `restorative` puts something back or grants something. */
type ActionTone = 'severe' | 'corrective' | 'judgement' | 'restorative'

const ACTION_TONE: Record<string, ActionTone> = {
  ban_user: 'severe',
  delete_article: 'severe',
  delete_post: 'severe',
  delete_thread: 'severe',
  suppress_revision: 'severe',
  revert_article: 'corrective',
  demote_mod: 'corrective',
  resolve_report: 'judgement',
  dismiss_report: 'judgement',
  restore_article: 'restorative',
  restore_post: 'restorative',
  restore_revision: 'restorative',
  restore_thread: 'restorative',
  unban_user: 'restorative',
  promote_mod: 'restorative',
}

const TONE_LEGEND: { tone: ActionTone; label: string }[] = [
  { tone: 'severe', label: 'Removed' },
  { tone: 'corrective', label: 'Corrected' },
  { tone: 'judgement', label: 'Report closed' },
  { tone: 'restorative', label: 'Restored' },
]

/** Page numbers to show around `page`, with nulls standing for a gap.
 *
 *  Always the first and last page plus a window around the current one, so
 *  the control keeps a fixed width however deep the log gets — jumping to
 *  the end stays one click at page 3 and at page 300. */
function pageItems(page: number, pages: number): (number | null)[] {
  if (pages <= 7) {
    return Array.from({ length: pages }, (_, i) => i + 1)
  }
  const items: (number | null)[] = [1]
  const from = Math.max(2, Math.min(page - 1, pages - 4))
  const to = Math.min(pages - 1, Math.max(page + 1, 5))
  if (from > 2) items.push(null)
  for (let n = from; n <= to; n += 1) items.push(n)
  if (to < pages - 1) items.push(null)
  items.push(pages)
  return items
}

function Pagination({
  page,
  pages,
  onGo,
}: {
  page: number
  pages: number
  onGo: (page: number) => void
}) {
  if (pages <= 1) return null
  return (
    <nav className="mod-pager" aria-label="Audit log pages">
      <button
        type="button"
        className="mod-pager-step"
        disabled={page === 1}
        onClick={() => onGo(page - 1)}
        aria-label="Previous page"
      >
        ‹
      </button>
      {pageItems(page, pages).map((n, i) =>
        n === null ? (
          // Index keys are wrong for anything reorderable, but a gap has no
          // identity of its own and there are at most two.
          <span key={`gap-${i}`} className="mod-pager-gap" aria-hidden="true">
            …
          </span>
        ) : (
          <button
            key={n}
            type="button"
            className={`mod-pager-page${n === page ? ' active' : ''}`}
            aria-current={n === page ? 'page' : undefined}
            onClick={() => onGo(n)}
          >
            {n}
          </button>
        ),
      )}
      <button
        type="button"
        className="mod-pager-step"
        disabled={page === pages}
        onClick={() => onGo(page + 1)}
        aria-label="Next page"
      >
        ›
      </button>
    </nav>
  )
}

function AuditTab() {
  const [data, setData] = useState<ModAuditPage | null>(null)
  const [error, setError] = useState(false)
  const [offset, setOffset] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    fetchModAudit(offset, controller.signal)
      .then(setData)
      .catch(() => {
        if (!controller.signal.aborted) setError(true)
      })
    return () => controller.abort()
  }, [offset])

  if (error) return <p className="mod-note">Could not load the audit log.</p>
  if (!data) return <p className="mod-note">Loading…</p>
  if (data.total === 0) {
    return <p className="mod-note">No moderator actions yet.</p>
  }

  const pages = Math.max(1, Math.ceil(data.total / data.page_size))
  // From the server's clamped offset, not our requested one, so the
  // highlighted page always matches the rows on screen.
  const page = Math.floor(data.offset / data.page_size) + 1
  const first = data.offset + 1
  const last = data.offset + data.actions.length

  return (
    <div className="mod-audit">
      <p className="mod-note">
        Every moderator action, newest first — the lens that catches a burst
        of removals no single user’s history would reveal. Showing{' '}
        {first}–{last} of {data.total}.
      </p>
      <ul className="mod-audit-key">
        {TONE_LEGEND.map(({ tone, label }) => (
          <li key={tone} className={`mod-audit-${tone}`}>
            {label}
          </li>
        ))}
      </ul>
      <ul className="mod-audit-list">
        {data.actions.map((a) => (
          <li
            key={a.id}
            className={
              ACTION_TONE[a.action] ? `mod-audit-${ACTION_TONE[a.action]}` : ''
            }
          >
            <span className="mod-audit-when">{when(a.created)}</span>{' '}
            <strong>{a.actor ?? '—'}</strong>{' '}
            {ACTION_LABEL[a.action] ?? a.action}
            {a.target_user && <> · {a.target_user}</>}
            {a.place_slug && (
              <>
                {' '}
                <a href={`/place/${a.place_slug}`}>/place/{a.place_slug}</a>
              </>
            )}
            {a.reason && <> — “{a.reason}”</>}
          </li>
        ))}
      </ul>
      <Pagination
        page={page}
        pages={pages}
        onGo={(next) => setOffset((next - 1) * data.page_size)}
      />
    </div>
  )
}

// --- dashboard shell ------------------------------------------------

function ModerationDashboard({ user }: { user: User | null }) {
  const [tab, setTab] = useState<'users' | 'reporters' | 'audit'>('users')
  return (
    <div className="mod-dashboard">
      <div className="mod-dash-tabs">
        <button
          type="button"
          className={tab === 'users' ? 'active' : ''}
          onClick={() => setTab('users')}
        >
          Users
        </button>
        <button
          type="button"
          className={tab === 'reporters' ? 'active' : ''}
          onClick={() => setTab('reporters')}
        >
          Reporters
        </button>
        <button
          type="button"
          className={tab === 'audit' ? 'active' : ''}
          onClick={() => setTab('audit')}
        >
          Audit
        </button>
      </div>
      <div className="mod-dash-body">
        {tab === 'users' && <UsersTab isAdmin={!!user?.is_admin} />}
        {tab === 'reporters' && <ReportersTab />}
        {tab === 'audit' && <AuditTab />}
      </div>
    </div>
  )
}

export default ModerationDashboard
