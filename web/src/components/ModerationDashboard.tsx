import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  banUser,
  fetchModReporters,
  fetchModUser,
  fetchModUsers,
  setUserRole,
  unbanUser,
} from '../api'
import { REPORT_CATEGORIES } from '../types'
import type {
  ModReporter,
  ModUserDetail,
  ModUserRow,
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

function BanForm({
  user,
  onDone,
}: {
  user: ModUserDetail
  onDone: () => void
}) {
  const [reason, setReason] = useState('')
  const [expiryDays, setExpiryDays] = useState(0)
  const [removeContent, setRemoveContent] = useState(false)
  const [busy, setBusy] = useState(false)

  const submit = () => {
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
      <label className="mod-ban-check">
        <input
          type="checkbox"
          checked={removeContent}
          onChange={(e) => setRemoveContent(e.target.checked)}
        />
        Also remove all their content
      </label>
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

function BanPanel({
  user,
  onChanged,
}: {
  user: ModUserDetail
  onChanged: () => void
}) {
  const [busy, setBusy] = useState(false)
  const activeBan = user.bans.find((b) => b.active)

  const unban = () => {
    setBusy(true)
    unbanUser(user.id)
      .then(onChanged)
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
  return <BanForm user={user} onDone={onChanged} />
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

// --- user detail ----------------------------------------------------

function UserDetail({
  userId,
  onChanged,
}: {
  userId: number
  onChanged: () => void
}) {
  const [detail, setDetail] = useState<ModUserDetail | null>(null)
  const [error, setError] = useState(false)

  const load = useCallback(() => {
    setDetail(null)
    setError(false)
    fetchModUser(userId)
      .then(setDetail)
      .catch(() => setError(true))
  }, [userId])

  useEffect(load, [load])

  if (error) return <p className="mod-note">Could not load this user.</p>
  if (!detail) return <p className="mod-note">Loading…</p>

  const refresh = () => {
    load()
    onChanged()
  }

  return (
    <div className="mod-user-detail">
      <h3>
        {detail.username}
        <RoleBadge role={detail.role} />
      </h3>
      <p className="mod-note">Joined {when(detail.date_joined)}</p>

      <BanPanel user={detail} onChanged={refresh} />
      <RolePanel user={detail} onChanged={refresh} />

      <section>
        <h4>Reports against them ({detail.reports_against.length})</h4>
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
      </section>

      <section>
        <h4>Talk posts ({detail.talk_posts.length})</h4>
        <ul className="mod-detail-list">
          {detail.talk_posts.map((p) => (
            <li key={p.id} className={p.deleted ? 'mod-removed' : ''}>
              {p.deleted && <span className="mod-removed-tag">removed</span>}{' '}
              <a href={`/place/${p.slug}`}>{p.place}</a> — “{p.thread_title}”
              <blockquote>{p.body_md}</blockquote>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h4>Revisions ({detail.revisions.length})</h4>
        <ul className="mod-detail-list">
          {detail.revisions.map((r) => (
            <li key={r.id} className={r.suppressed ? 'mod-removed' : ''}>
              {r.suppressed && (
                <span className="mod-removed-tag">suppressed</span>
              )}{' '}
              {r.is_current && <span className="mod-current">current</span>}{' '}
              <a href={`/place/${r.slug}`}>{r.place}</a>
              {r.excerpt && <blockquote>{r.excerpt}</blockquote>}
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h4>Actions taken ({detail.audit.length})</h4>
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
      </section>
    </div>
  )
}

// --- users tab ------------------------------------------------------

function UsersTab() {
  const [rows, setRows] = useState<ModUserRow[] | null>(null)
  const [filter, setFilter] = useState('')
  const [selected, setSelected] = useState<number | null>(null)

  const load = useCallback(() => {
    fetchModUsers()
      .then(setRows)
      .catch(console.error)
  }, [])

  useEffect(load, [load])

  const shown = useMemo(() => {
    if (!rows) return []
    const q = filter.trim().toLowerCase()
    return q ? rows.filter((r) => r.username.toLowerCase().includes(q)) : rows
  }, [rows, filter])

  return (
    <div className="mod-users">
      <div className="mod-users-list">
        <input
          className="mod-filter"
          value={filter}
          placeholder="Filter users…"
          onChange={(e) => setFilter(e.target.value)}
        />
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
          <UserDetail userId={selected} onChanged={load} />
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

// --- dashboard shell ------------------------------------------------

function ModerationDashboard() {
  const [tab, setTab] = useState<'users' | 'reporters'>('users')
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
      </div>
      <div className="mod-dash-body">
        {tab === 'users' ? <UsersTab /> : <ReportersTab />}
      </div>
    </div>
  )
}

export default ModerationDashboard
