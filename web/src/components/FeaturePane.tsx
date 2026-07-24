import { useEffect, useState } from 'react'
import {
  deleteArticle,
  getPlace,
  resolveFeature,
  restoreArticle,
  setProtection,
} from '../api'
import type {
  ArticleData,
  ClickContext,
  FeatureCandidate,
  PlaceDetail,
  ProtectionLevel,
  ResolvedPlace,
  User,
} from '../types'
import ArticleEditor from './ArticleEditor'
import ArticleView from './ArticleView'
import HistoryTab from './HistoryTab'
import TalkTab from './TalkTab'

// Feather icons (MIT), inlined as SVG paths; `currentColor` lets CSS grey them.
const ICON = {
  width: 17,
  height: 17,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
} as const

function CrosshairIcon() {
  return (
    <svg {...ICON} aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <line x1="12" y1="1.5" x2="12" y2="6" />
      <line x1="12" y1="18" x2="12" y2="22.5" />
      <line x1="1.5" y1="12" x2="6" y2="12" />
      <line x1="18" y1="12" x2="22.5" y2="12" />
    </svg>
  )
}

function LinkIcon() {
  return (
    <svg {...ICON} aria-hidden="true">
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </svg>
  )
}

function CheckIcon() {
  return (
    <svg {...ICON} aria-hidden="true">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  )
}

function CloseIcon() {
  return (
    <svg {...ICON} aria-hidden="true">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}

const PROTECTION_NOTE: Record<ProtectionLevel, string> = {
  none: '',
  registered: 'Semi-protected — registered users only.',
  admin: 'Protected — only moderators can edit this article.',
}

interface FeaturePaneProps {
  feature: FeatureCandidate
  click: ClickContext
  user: User | null
  /** The selected place has scrolled out of the map viewport. */
  offView: boolean
  onRequestAuth: () => void
  onClose: () => void
  /** Fly the map back to this place (offered when offView). */
  onRecenter: () => void
  /** Follow an in-article link to another place, by slug. */
  onSelectSlug: (slug: string) => void
  onArticleSaved: () => void
  /** Fires once the click/slug settles into a Place — App syncs the
   *  /place/<slug> URL and the document title off this. */
  onResolved: (place: ResolvedPlace) => void
}

type Resolution =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'done'; place: ResolvedPlace }

type Detail =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'done'; detail: PlaceDetail }

type Tab = 'article' | 'talk' | 'history' | 'edit'

const TABS: { id: Tab; label: string }[] = [
  { id: 'article', label: 'Article' },
  { id: 'talk', label: 'Talk' },
  { id: 'history', label: 'History' },
]

const ANCHOR_LABEL: Record<ResolvedPlace['anchor_level'], string> = {
  wikidata: 'Anchored to Wikidata',
  osm: 'Anchored to OpenStreetMap',
  name: 'Anchored by name and location',
}

function AnchorInfo({
  resolution,
  moderator,
}: {
  resolution: Resolution
  moderator: boolean
}) {
  if (resolution.status === 'loading') {
    return <p className="anchor-info anchor-pending">Resolving place…</p>
  }
  if (resolution.status === 'error') {
    return (
      <p className="anchor-info anchor-error">
        Could not resolve this feature to a place right now.
      </p>
    )
  }
  // Anchor plumbing (QID, OSM ref, slug) is moderator-only.
  if (!moderator) return null
  const { place } = resolution
  return (
    <div className="anchor-info">
      <span className={`anchor-badge anchor-${place.anchor_level}`}>
        {ANCHOR_LABEL[place.anchor_level]}
      </span>
      <div className="anchor-links">
        {place.wikidata_qid && (
          <a
            href={`https://www.wikidata.org/wiki/${place.wikidata_qid}`}
            target="_blank"
            rel="noreferrer"
          >
            {place.wikidata_qid}
          </a>
        )}
        {place.osm_type && place.osm_id && (
          <a
            href={`https://www.openstreetmap.org/${place.osm_type}/${place.osm_id}`}
            target="_blank"
            rel="noreferrer"
          >
            {place.osm_type}/{place.osm_id}
          </a>
        )}
        <span className="anchor-slug">/place/{place.slug}</span>
      </div>
    </div>
  )
}

function ProtectionControl({
  slug,
  level,
  onChange,
}: {
  slug: string
  level: ProtectionLevel
  onChange: (level: ProtectionLevel) => void
}) {
  const [busy, setBusy] = useState(false)
  return (
    <label className="protection-control">
      Protection
      <select
        value={level}
        disabled={busy}
        onChange={(event) => {
          const next = event.target.value as ProtectionLevel
          setBusy(true)
          setProtection(slug, next)
            .then(onChange)
            .catch(console.error)
            .finally(() => setBusy(false))
        }}
      >
        {/* `registered` is omitted: with anonymous editing disallowed
            globally it gates the same set as `none`. Kept in the model
            (and shown if a legacy article still carries it) for a future
            where anonymous editing exists. */}
        <option value="none">Unprotected</option>
        {level === 'registered' && (
          <option value="registered">Registered users</option>
        )}
        <option value="admin">Moderators only</option>
      </select>
    </label>
  )
}

/** Admin-only whole-article deletion (DESIGN.md M13). Deliberately not an
 *  icon button next to Close: this is the one action in the pane that takes
 *  the whole article off the map, so it wants a reason and a confirm. */
function DeleteControl({
  slug,
  onDeleted,
}: {
  slug: string
  onDeleted: () => void
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)

  if (!open) {
    return (
      <button
        type="button"
        className="article-delete-button"
        onClick={() => setOpen(true)}
      >
        Delete article
      </button>
    )
  }
  return (
    <div className="article-delete-form">
      <p className="feature-pane-note">
        The place becomes a stub. Every revision is kept and you can restore
        it — but anyone writing a new article here also brings the old
        history back, so suppress an abusive revision separately.
      </p>
      <input
        className="article-delete-reason"
        placeholder="Reason (recorded in the audit log)"
        value={reason}
        onChange={(event) => setReason(event.target.value)}
      />
      <div className="article-delete-actions">
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setBusy(true)
            deleteArticle(slug, reason)
              .then(onDeleted)
              .catch(console.error)
              .finally(() => setBusy(false))
          }}
        >
          Delete article
        </button>
        <button type="button" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </div>
  )
}

function FeaturePane({
  feature,
  click,
  user,
  offView,
  onRequestAuth,
  onClose,
  onRecenter,
  onSelectSlug,
  onArticleSaved,
  onResolved,
}: FeaturePaneProps) {
  const [resolution, setResolution] = useState<Resolution>({
    status: 'loading',
  })
  const [detail, setDetail] = useState<Detail>({ status: 'loading' })
  const [tab, setTab] = useState<Tab>('article')
  const [wide, setWide] = useState(false)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setResolution({ status: 'loading' })
    setDetail({ status: 'loading' })
    setTab('article')
    // An article dot already knows its place: skip resolution entirely.
    const detailPromise = feature.slug
      ? getPlace(feature.slug, controller.signal).then((placeDetail) => {
          setResolution({ status: 'done', place: placeDetail.place })
          onResolved(placeDetail.place)
          return placeDetail
        })
      : resolveFeature(
          // Overpass matches OSM `name`; the English name rides along
          // so the place shows it as display_name.
          feature.rawName ?? feature.name,
          feature.kind,
          feature.anchor ? { ...click, lngLat: feature.anchor } : click,
          feature.nameEn ?? null,
          controller.signal,
        )
          .then((response) => {
            setResolution({ status: 'done', place: response.place })
            onResolved(response.place)
            return getPlace(response.place.slug, controller.signal)
          })
    detailPromise
      .then((placeDetail) => setDetail({ status: 'done', detail: placeDetail }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          console.error(error)
          setResolution((prev) =>
            prev.status === 'done' ? prev : { status: 'error' },
          )
          setDetail({ status: 'error' })
        }
      })
    return () => controller.abort()
  }, [feature, click, onResolved])

  const handleCopyLink = () => {
    if (!place) return
    // Clean permalink — no #zoom/lat/lng hash, so it restores the place's
    // own default view (and is what you paste into an article's markdown).
    const url = `${window.location.origin}/place/${place.slug}`
    navigator.clipboard
      ?.writeText(url)
      .then(() => {
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      })
      .catch(console.error)
  }

  const handleSaved = (article: ArticleData) => {
    setDetail((prev) =>
      prev.status === 'done'
        ? { status: 'done', detail: { ...prev.detail, article } }
        : prev,
    )
    onArticleSaved()
  }

  const handleProtectionChange = (level: ProtectionLevel) => {
    setDetail((prev) =>
      prev.status === 'done'
        ? {
            status: 'done',
            detail: { ...prev.detail, protection_level: level },
          }
        : prev,
    )
  }

  // Delete and restore both change what the whole pane shows (article ⇄
  // stub) and what the map highlights, so refetch rather than patch state.
  const reloadDetail = (slug: string) => {
    getPlace(slug)
      .then((fresh) => setDetail({ status: 'done', detail: fresh }))
      .catch(() => setDetail({ status: 'error' }))
    onArticleSaved()
  }

  const handleRestore = (slug: string) => {
    restoreArticle(slug)
      .then(() => reloadDetail(slug))
      .catch(console.error)
  }

  const place = resolution.status === 'done' ? resolution.place : null
  const article = detail.status === 'done' ? detail.detail.article : null
  const deleted = detail.status === 'done' ? detail.detail.deleted : null
  const protection: ProtectionLevel =
    detail.status === 'done' ? detail.detail.protection_level : 'none'
  // `admin` protection restricts edits/reverts to moderators; anonymous
  // editing is disallowed everywhere, so other levels gate the same set.
  const canEdit = !!user && (protection !== 'admin' || user.is_moderator)

  return (
    <aside className={`feature-pane${wide ? ' wide' : ''}`}>
      <div className="feature-pane-header">
        <div>
          <span className="feature-kind">{feature.kind}</span>
          <h1>{place ? place.display_name : feature.name}</h1>
        </div>
        <div className="feature-pane-actions">
          {place && offView && (
            <button
              type="button"
              className="pane-icon-button"
              onClick={onRecenter}
              title="Zoom to place"
              aria-label="Zoom to place"
            >
              <CrosshairIcon />
            </button>
          )}
          {place && (
            <button
              type="button"
              className="pane-icon-button"
              onClick={handleCopyLink}
              title="Copy link to place"
              aria-label="Copy link to place"
            >
              {copied ? <CheckIcon /> : <LinkIcon />}
            </button>
          )}
          <button
            type="button"
            className="pane-icon-button"
            onClick={onClose}
            aria-label="Close"
          >
            <CloseIcon />
          </button>
        </div>
      </div>
      <AnchorInfo resolution={resolution} moderator={!!user?.is_moderator} />

      {place && (
        <nav className="pane-tabs">
          {TABS.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              className={`pane-tab${tab === id ? ' active' : ''}`}
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
          {canEdit && (
            <button
              type="button"
              className={`pane-tab pane-tab-edit${tab === 'edit' ? ' active' : ''}`}
              onClick={() => setTab('edit')}
            >
              Edit
            </button>
          )}
        </nav>
      )}

      {place && tab === 'talk' && (
        <TalkTab slug={place.slug} user={user} onRequestAuth={onRequestAuth} />
      )}

      {place && tab === 'history' && (
        <HistoryTab
          slug={place.slug}
          user={user}
          canEdit={canEdit}
          onReverted={handleSaved}
          onWideChange={setWide}
        />
      )}

      {place && tab === 'edit' && (
        <ArticleEditor
          slug={place.slug}
          displayName={place.display_name}
          initial={article?.content ?? null}
          onSaved={(saved) => {
            handleSaved(saved)
            setTab('article')
          }}
          onCancel={() => setTab('article')}
        />
      )}

      {place && tab === 'article' && detail.status === 'loading' && (
        <p className="feature-pane-note">Loading article…</p>
      )}

      {place && tab === 'article' && protection !== 'none' && (
        <p className="protection-note">🔒 {PROTECTION_NOTE[protection]}</p>
      )}

      {place && tab === 'article' && user?.is_moderator && (
        <ProtectionControl
          slug={place.slug}
          level={protection}
          onChange={handleProtectionChange}
        />
      )}

      {/* Admin-only, and only ever rendered on a deleted article — for
          everyone else `deleted` is null and this place is just a stub. */}
      {place && tab === 'article' && deleted && (
        <div className="article-deleted-banner">
          <p>
            <strong>Deleted article.</strong> Removed by {deleted.by ?? '—'}{' '}
            on {new Date(deleted.at).toLocaleDateString()}. Only admins can
            see this; the place reads as a stub to everyone else.
          </p>
          <button
            type="button"
            className="article-restore-button"
            onClick={() => handleRestore(place.slug)}
          >
            Restore article
          </button>
        </div>
      )}

      {place && tab === 'article' && article && (
        <ArticleView article={article} onSelectSlug={onSelectSlug} />
      )}

      {place && tab === 'article' && article && !deleted && user?.is_admin && (
        <DeleteControl
          slug={place.slug}
          onDeleted={() => reloadDetail(place.slug)}
        />
      )}

      {place && tab === 'article' && detail.status === 'done' && !article && (
        <div className="article-stub">
          <p className="feature-pane-note">
            No article about this place name yet.
          </p>
          {canEdit ? (
            <button
              type="button"
              className="article-write-button"
              onClick={() => setTab('edit')}
            >
              Write this article
            </button>
          ) : user ? (
            <p className="feature-pane-note">
              This place is protected — only moderators can start it.
            </p>
          ) : (
            <button
              type="button"
              className="article-write-button"
              onClick={onRequestAuth}
            >
              Log in to write this article
            </button>
          )}
          <table className="feature-props">
            <tbody>
              {Object.entries(feature.properties).map(([key, value]) => (
                <tr key={key}>
                  <th>{key}</th>
                  <td>{String(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </aside>
  )
}

export default FeaturePane
