/**
 * Which points of interest count as toponyms.
 *
 * A restaurant named OJAI is a *business* name, not a place name — but
 * nothing used to stop one becoming a Place, and the first to claim a name
 * takes the clean slug permanently (there is no rename path). So commercial
 * POIs are kept out of the two surfaces that can mint one: the map's
 * clickable features, and the search box's geocoder results.
 *
 * Both lists are **allowlists on purpose**. OpenStreetMap gains categories
 * over time, and a denylist silently admits every one we haven't heard of —
 * which is precisely how the restaurant got in. Failing closed means a new
 * category is invisible until we add it here, which on a toponymy wiki is
 * the right direction to be wrong.
 *
 * The two lists say the same thing in different vocabularies and cannot be
 * shared: the map filters OpenMapTiles' derived `class`, while Photon
 * returns raw OSM tags. **Change one, change the other** — see
 * `PHOTON_TAG_ALLOWLIST` below.
 */

import type { ExpressionSpecification, FilterSpecification } from 'maplibre-gl'

/**
 * OpenMapTiles `poi` classes that stay on the map. Verified against the
 * OpenFreeMap Liberty sprite sheet, where the icon name *is* the class.
 *
 * To show more, add here and to `PHOTON_TAG_ALLOWLIST`: likely candidates
 * are 'monument', 'museum' and 'art_gallery'.
 */
export const POI_CLASS_ALLOWLIST = ['castle', 'lighthouse', 'attraction']

/**
 * Rail stations, allowed alongside the list above.
 *
 * Kept because a station is not reliably "a facility named after the place
 * it serves": Cork Kent is named for Thomas Kent (executed 1916) and Gare
 * Saint-Lazare, Austerlitz and Waterloo all carry etymologies of their own.
 * A category-wide exclusion would lose every one of those to catch the
 * narrow case where a station's name exactly matches its district's — and
 * that case is handled where it actually bites, by ranking the toponym
 * first in the picker (`features.ts`) and in search (`isToponymicPhotonHit`
 * plus the dedupe in `searchGeocoder`).
 *
 * The class is **`railway`**, not `rail` — the style's own `poi_transit`
 * layer filters on `["airport", "bus", "rail"]` and so matches no station
 * in current planet tiles at all; stations reach the map through the
 * rank-banded `poi_r1`/`poi_r7`/`poi_r20` layers (z15/16/17), which is
 * also a better zoom gate than anything we would pick by hand.
 *
 * `subclass` is the raw OSM value. **`station` only** — `halt` looks like it
 * belongs (OSM documents it as a small passenger station, the British
 * "Bearsted Halt" sense) but is in practice used for light rail: one central
 * Portland tile holds 24 halts and 23 tram stops, the same MAX system tagged
 * both ways, named for the crossings they sit on ("Library/Southwest 9th
 * Avenue"). Nothing is lost by excluding it — even a tiny rural station is
 * tagged `station` (Cantley, Norfolk), so the small-station case the name
 * suggests is already covered. `subway`, `tram_stop` and platforms are out
 * for the same reason.
 */
const RAILWAY_CLASS = 'railway'
const RAILWAY_SUBCLASSES = ['station']

/**
 * Bus stops stay out of both surfaces: they are numerous — 221 in one
 * central-Paris tile against 28 railway features — essentially always
 * named for the street or place they sit on, and carry no etymology of
 * their own.
 */

/**
 * The same set as raw OSM key → values, for Photon hits.
 *
 * Keys absent here (amenity, shop, office, craft, healthcare, …) are
 * dropped wholesale, matching the map: none of their classes render.
 * Keys not listed *and* not commercial — place, waterway, natural,
 * boundary, highway, railway, aeroway, landuse, leisure — are the
 * toponymic bulk of the geocoder and pass through untouched, so this
 * gates only the mixed keys.
 */
const PHOTON_TAG_ALLOWLIST: Record<string, string[]> = {
  historic: ['castle'],
  man_made: ['lighthouse'],
  tourism: ['attraction'],
}

/**
 * Transit values dropped even though their key is otherwise toponymic.
 *
 * `railway=stop` is the operational stop node beside a station and simply
 * duplicates it — Photon returns four rows for Cork Kent, three of them
 * these. The rest are the bus/tram/platform furniture the map also hides.
 */
const PHOTON_TRANSIT_DENY: Record<string, string[]> = {
  railway: ['halt', 'stop', 'tram_stop', 'subway_entrance', 'platform'],
  highway: ['bus_stop', 'platform'],
  public_transport: ['stop_position', 'platform', 'stop_area'],
}

/** Keys whose every value is a business or an institution, never a toponym. */
const PHOTON_COMMERCIAL_KEYS = new Set([
  'amenity',
  'shop',
  'office',
  'craft',
  'healthcare',
  'emergency',
])

/** True when a geocoder hit may become a Place. */
export function isToponymicPhotonHit(key: string, value: string): boolean {
  if (PHOTON_COMMERCIAL_KEYS.has(key)) return false
  if (PHOTON_TRANSIT_DENY[key]?.includes(value)) return false
  const allowed = PHOTON_TAG_ALLOWLIST[key]
  return allowed ? allowed.includes(value) : true
}

/**
 * Filter restricting a `poi` layer to the allowlist. ANDed onto the
 * layer's own filter (rank bands, geometry type) rather than replacing it.
 */
export function poiClassFilter(
  existing?: FilterSpecification,
): FilterSpecification {
  const allow: ExpressionSpecification = [
    'any',
    ['match', ['get', 'class'], POI_CLASS_ALLOWLIST, true, false],
    [
      'all',
      ['==', ['get', 'class'], RAILWAY_CLASS],
      ['match', ['get', 'subclass'], RAILWAY_SUBCLASSES, true, false],
    ],
  ]
  if (!existing) return allow
  // `all` is typed as all-expression or all-legacy, and `existing` is the
  // broad union of both, so the mix can't be proven here. Safe in fact:
  // the poi layers ship expression filters (`["all", ["match", ...]]`).
  return ['all', existing, allow] as FilterSpecification
}
