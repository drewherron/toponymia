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
 */
export function toCandidates(
  features: MapGeoJSONFeature[],
): FeatureCandidate[] {
  const seen = new Set<string>()
  const candidates: FeatureCandidate[] = []
  for (const feature of features) {
    const name = feature.properties?.name
    if (typeof name !== 'string' || !name) continue
    const kind = kindOf(feature)
    const key = `${name}|${kind}`
    if (seen.has(key)) continue
    seen.add(key)
    candidates.push({
      name,
      kind,
      sourceLayer: feature.sourceLayer ?? '',
      properties: { ...feature.properties },
    })
  }
  return candidates
}
