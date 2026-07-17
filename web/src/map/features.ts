import type { MapGeoJSONFeature } from 'maplibre-gl'
import type { FeatureCandidate } from '../types'

const KIND_BY_SOURCE_LAYER: Record<string, string> = {
  transportation_name: 'road',
  water_name: 'water',
  waterway: 'waterway',
  boundary: 'boundary',
  mountain_peak: 'peak',
  aerodrome_label: 'aerodrome',
  poi: 'poi',
}

// Mirrors MapView's NAME_FIELD label expression, so the picker/pane show
// the same English-first name the map renders.
const NAME_KEYS = ['name:en', 'name_en', 'name:latin', 'name']

function displayNameOf(props: Record<string, unknown>): string | null {
  for (const key of NAME_KEYS) {
    const value = props[key]
    if (typeof value === 'string' && value) return value
  }
  return null
}

function kindOf(feature: MapGeoJSONFeature): string {
  const sourceLayer = feature.sourceLayer ?? ''
  if (sourceLayer === 'place' || sourceLayer === 'park') {
    const cls = feature.properties?.class
    if (typeof cls === 'string' && cls) return cls
  }
  return KIND_BY_SOURCE_LAYER[sourceLayer] ?? sourceLayer
}

/**
 * Collapse raw rendered features (one per layer fragment: casing, line,
 * label, ...) into named, deduplicated candidates for the picker.
 *
 * Article dots come first: they carry the place's slug, so selecting one
 * skips the resolve round-trip and opens the article directly.
 */
export function toCandidates(
  features: MapGeoJSONFeature[],
  dots: MapGeoJSONFeature[] = [],
): FeatureCandidate[] {
  const seen = new Set<string>()
  const candidates: FeatureCandidate[] = []
  for (const dot of dots) {
    const props = dot.properties ?? {}
    const name = props.display_name
    const slug = props.slug
    if (typeof name !== 'string' || typeof slug !== 'string') continue
    const kind =
      typeof props.feature_class === 'string' && props.feature_class
        ? props.feature_class
        : 'place'
    const key = `${name}|${kind}`
    if (seen.has(key)) continue
    seen.add(key)
    candidates.push({
      name,
      kind,
      sourceLayer: 'article-dots',
      slug,
      properties: { ...props },
    })
  }
  for (const feature of features) {
    const rawName = feature.properties?.name
    if (typeof rawName !== 'string' || !rawName) continue
    const name = displayNameOf(feature.properties) ?? rawName
    const kind = kindOf(feature)
    const key = `${name}|${kind}`
    if (seen.has(key)) continue
    seen.add(key)
    let anchor: FeatureCandidate['anchor']
    if (feature.geometry.type === 'Point') {
      const [lng, lat] = feature.geometry.coordinates
      anchor = { lng, lat }
    }
    candidates.push({
      name,
      rawName,
      kind,
      sourceLayer: feature.sourceLayer ?? '',
      properties: { ...feature.properties },
      anchor,
    })
  }
  return candidates
}
