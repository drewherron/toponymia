import { useEffect, useState } from 'react'
import { getPlace, resolveFeature, setProtection } from '../api'
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

const PROTECTION_NOTE: Record<ProtectionLevel, string> = {
  none: '',
  registered: 'Semi-protected — registered users only.',
  admin: 'Protected — only moderators can edit this article.',
}

interface FeaturePaneProps {
  feature: FeatureCandidate
  click: ClickContext
  user: User | null
  onRequestAuth: () => void
  onClose: () => void
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

type Tab = 'article' | 'talk' | 'history'

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
        <option value="none">None</option>
        <option value="registered">Registered users</option>
        <option value="admin">Moderators only</option>
      </select>
    </label>
  )
}

function FeaturePane({
  feature,
  click,
  user,
  onRequestAuth,
  onClose,
  onArticleSaved,
  onResolved,
}: FeaturePaneProps) {
  const [resolution, setResolution] = useState<Resolution>({
    status: 'loading',
  })
  const [detail, setDetail] = useState<Detail>({ status: 'loading' })
  const [editing, setEditing] = useState(false)
  const [tab, setTab] = useState<Tab>('article')
  const [wide, setWide] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setResolution({ status: 'loading' })
    setDetail({ status: 'loading' })
    setEditing(false)
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

  const handleSaved = (article: ArticleData) => {
    setDetail((prev) =>
      prev.status === 'done'
        ? { status: 'done', detail: { ...prev.detail, article } }
        : prev,
    )
    setEditing(false)
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

  const place = resolution.status === 'done' ? resolution.place : null
  const article = detail.status === 'done' ? detail.detail.article : null
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
        <button
          type="button"
          className="feature-pane-close"
          onClick={onClose}
          aria-label="Close"
        >
          ×
        </button>
      </div>
      <AnchorInfo resolution={resolution} moderator={!!user?.is_moderator} />

      {place && !editing && (
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
        </nav>
      )}

      {place && !editing && tab === 'talk' && (
        <TalkTab slug={place.slug} user={user} onRequestAuth={onRequestAuth} />
      )}

      {place && !editing && tab === 'history' && (
        <HistoryTab
          slug={place.slug}
          user={user}
          canEdit={canEdit}
          onReverted={handleSaved}
          onWideChange={setWide}
        />
      )}

      {place && editing && (
        <ArticleEditor
          slug={place.slug}
          displayName={place.display_name}
          initial={article?.content ?? null}
          onSaved={handleSaved}
          onCancel={() => setEditing(false)}
        />
      )}

      {place && !editing && tab === 'article' && detail.status === 'loading' && (
        <p className="feature-pane-note">Loading article…</p>
      )}

      {place && !editing && tab === 'article' && protection !== 'none' && (
        <p className="protection-note">🔒 {PROTECTION_NOTE[protection]}</p>
      )}

      {place && !editing && tab === 'article' && user?.is_moderator && (
        <ProtectionControl
          slug={place.slug}
          level={protection}
          onChange={handleProtectionChange}
        />
      )}

      {place && !editing && tab === 'article' && article && (
        <>
          {canEdit && (
            <button
              type="button"
              className="article-edit-button"
              onClick={() => setEditing(true)}
            >
              Edit article
            </button>
          )}
          <ArticleView article={article} />
        </>
      )}

      {place && !editing && tab === 'article' && detail.status === 'done' && !article && (
        <div className="article-stub">
          <p className="feature-pane-note">
            No article about this place name yet.
          </p>
          {canEdit ? (
            <button
              type="button"
              className="article-write-button"
              onClick={() => setEditing(true)}
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
