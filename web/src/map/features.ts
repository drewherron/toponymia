import type { ExpressionSpecification, MapGeoJSONFeature } from 'maplibre-gl'
import type { FeatureCandidate } from '../types'
import { RAILWAY_CLASS } from '../poi'
import { displayNameOf } from './labels'

const KIND_BY_SOURCE_LAYER: Record<string, string> = {
  transportation_name: 'road',
  water_name: 'water',
  waterway: 'waterway',
  boundary: 'boundary',
  mountain_peak: 'peak',
  aerodrome_label: 'aerodrome',
}

/**
 * Layers that mix kinds, so the kind is each feature's own `class` rather
 * than a constant for the layer: `place` (city vs country vs state), `park`
 * (national_park vs nature_reserve), and `poi`.
 *
 * `poi` is here so that a click reports *what kind of POI* — a castle, not
 * the word "poi". The server allowlist (`server/core/feature_classes.py`)
 * can only be a real rule if the value it checks names a category; while
 * this layer reported one constant, that constant had to be permitted
 * wholesale and a hand-written POST claiming it said nothing.
 */
const CLASS_BEARING_LAYERS = new Set(['place', 'park', 'poi'])

const STATION_KIND = 'station'

/**
 * Classes renamed on the way out, where the tile schema's word differs from
 * the one `kindFromPhoton()` (`../api.ts`) produces for the same feature.
 *
 * Stations are the only case: OpenMapTiles calls the class `railway`, while
 * Photon reports `railway=station` and so yields `station`. The two must
 * agree — the kind is half of the resolve cache key and half of the label
 * highlight token, so a map click and a search pick on one station have to
 * produce the same string or they mint two Places for it. Renaming is safe
 * rather than lossy because `poiClassFilter` only admits `railway` when its
 * subclass is `station` (see `../poi.ts`), so everything that reaches a
 * click is one.
 */
const KIND_ALIASES: Record<string, string> = { [RAILWAY_CLASS]: STATION_KIND }

export function kindOf(feature: MapGeoJSONFeature): string {
  const sourceLayer = feature.sourceLayer ?? ''
  if (CLASS_BEARING_LAYERS.has(sourceLayer)) {
    const cls = feature.properties?.class
    if (typeof cls === 'string' && cls) return KIND_ALIASES[cls] ?? cls
  }
  // A class-bearing feature that somehow carries no class falls through to
  // its layer's own name, which is in no allowlist — a visible 400 rather
  // than a Place of unknown category.
  return KIND_BY_SOURCE_LAYER[sourceLayer] ?? sourceLayer
}

/**
 * The class part of a label's highlight token, matched against a Place's
 * stored `feature_class` (which is exactly what `kindOf` reported at
 * resolve time). Mixed layers read each feature's own `class`; every other
 * layer carries a single fixed kind, so return it as a constant.
 *
 * The `match` mirrors `KIND_ALIASES` above — it has to, or a station's
 * article would be stored under `station` and its label looked up under
 * `railway`, and the highlight would never light.
 */
export function labelClassExpr(
  sourceLayer: string,
): ExpressionSpecification | string {
  if (CLASS_BEARING_LAYERS.has(sourceLayer)) {
    const cls: ExpressionSpecification = ['coalesce', ['get', 'class'], '']
    return ['match', cls, RAILWAY_CLASS, STATION_KIND, cls]
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
  lang = 'en',
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
    const name = displayNameOf(feature.properties, lang) ?? rawName
    // Always the *English* name, whatever language is displayed —
    // resolve titles new places with it (an English-language wiki).
    const nameEn = displayNameOf(feature.properties, 'en')
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
      nameEn: nameEn && nameEn !== rawName ? nameEn : undefined,
      kind,
      sourceLayer: feature.sourceLayer ?? '',
      properties: { ...feature.properties },
      anchor,
    })
  }
  return groupByName(candidates)
}

/** How strong a claim a candidate has on a name it shares with others.
 *  A station called "Paddington" is a real feature worth its own article
 *  (cf. Cork Kent), but the district is what someone clicking that name
 *  usually means — so the toponym leads and the POI sits under it. */
function nameRank(candidate: FeatureCandidate): number {
  if (candidate.sourceLayer === 'article-dots') return 0
  if (candidate.sourceLayer === 'place') return 1
  return 2
}

/** Keep same-named candidates adjacent and ranked, without disturbing the
 *  order distinct names arrived in (dots first, then the click's own
 *  layer order). A plain sort can't express "only reorder ties". */
function groupByName(candidates: FeatureCandidate[]): FeatureCandidate[] {
  const byName = new Map<string, FeatureCandidate[]>()
  for (const candidate of candidates) {
    const group = byName.get(candidate.name)
    if (group) group.push(candidate)
    else byName.set(candidate.name, [candidate])
  }
  const ordered: FeatureCandidate[] = []
  for (const candidate of candidates) {
    const group = byName.get(candidate.name)
    if (!group) continue
    byName.delete(candidate.name)
    ordered.push(...group.sort((a, b) => nameRank(a) - nameRank(b)))
  }
  return ordered
}
