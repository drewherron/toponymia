import { useEffect, useState } from 'react'
import { resolveFeature } from '../api'
import type { ClickContext, FeatureCandidate, ResolvedPlace } from '../types'

interface FeaturePaneProps {
  feature: FeatureCandidate
  click: ClickContext
  onClose: () => void
}

type Resolution =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'done'; place: ResolvedPlace }

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

function FeaturePane({ feature, click, onClose }: FeaturePaneProps) {
  const [resolution, setResolution] = useState<Resolution>({
    status: 'loading',
  })

  useEffect(() => {
    const controller = new AbortController()
    setResolution({ status: 'loading' })
    resolveFeature(feature.name, feature.kind, click, controller.signal)
      .then((response) => setResolution({ status: 'done', place: response.place }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          console.error(error)
          setResolution({ status: 'error' })
        }
      })
    return () => controller.abort()
  }, [feature, click])

  return (
    <aside className="feature-pane">
      <div className="feature-pane-header">
        <div>
          <span className="feature-kind">{feature.kind}</span>
          <h1>
            {resolution.status === 'done'
              ? resolution.place.display_name
              : feature.name}
          </h1>
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
      <p className="feature-pane-note">
        No article yet. Article view coming in a later milestone — below is the
        raw map feature.
      </p>
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
    </aside>
  )
}

export default FeaturePane
