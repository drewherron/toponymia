import { useEffect, useState } from 'react'
import { getPlace, resolveFeature } from '../api'
import type {
  ArticleData,
  ClickContext,
  FeatureCandidate,
  PlaceDetail,
  ResolvedPlace,
  User,
} from '../types'
import ArticleEditor from './ArticleEditor'
import ArticleView from './ArticleView'

interface FeaturePaneProps {
  feature: FeatureCandidate
  click: ClickContext
  user: User | null
  onRequestAuth: () => void
  onClose: () => void
}

type Resolution =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'done'; place: ResolvedPlace }

type Detail =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'done'; detail: PlaceDetail }

const ANCHOR_LABEL: Record<ResolvedPlace['anchor_level'], string> = {
  wikidata: 'Anchored to Wikidata',
  osm: 'Anchored to OpenStreetMap',
  name: 'Anchored by name and location',
}

function AnchorInfo({ resolution }: { resolution: Resolution }) {
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

function FeaturePane({
  feature,
  click,
  user,
  onRequestAuth,
  onClose,
}: FeaturePaneProps) {
  const [resolution, setResolution] = useState<Resolution>({
    status: 'loading',
  })
  const [detail, setDetail] = useState<Detail>({ status: 'loading' })
  const [editing, setEditing] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setResolution({ status: 'loading' })
    setDetail({ status: 'loading' })
    setEditing(false)
    resolveFeature(feature.name, feature.kind, click, controller.signal)
      .then((response) => {
        setResolution({ status: 'done', place: response.place })
        return getPlace(response.place.slug, controller.signal)
      })
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
  }, [feature, click])

  const handleSaved = (article: ArticleData) => {
    setDetail((prev) =>
      prev.status === 'done'
        ? { status: 'done', detail: { ...prev.detail, article } }
        : prev,
    )
    setEditing(false)
  }

  const place = resolution.status === 'done' ? resolution.place : null
  const article = detail.status === 'done' ? detail.detail.article : null

  return (
    <aside className="feature-pane">
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
      <AnchorInfo resolution={resolution} />

      {place && editing && (
        <ArticleEditor
          slug={place.slug}
          displayName={place.display_name}
          initial={article?.content ?? null}
          onSaved={handleSaved}
          onCancel={() => setEditing(false)}
        />
      )}

      {place && !editing && detail.status === 'loading' && (
        <p className="feature-pane-note">Loading article…</p>
      )}

      {place && !editing && article && (
        <>
          {user && (
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

      {place && !editing && detail.status === 'done' && !article && (
        <div className="article-stub">
          <p className="feature-pane-note">
            No article about this place name yet.
          </p>
          {user ? (
            <button
              type="button"
              className="article-write-button"
              onClick={() => setEditing(true)}
            >
              Write this article
            </button>
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
