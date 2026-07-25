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
    'match',
    ['get', 'class'],
    POI_CLASS_ALLOWLIST,
    true,
    false,
  ]
  if (!existing) return allow
  // `all` is typed as all-expression or all-legacy, and `existing` is the
  // broad union of both, so the mix can't be proven here. Safe in fact:
  // the poi layers ship expression filters (`["all", ["match", ...]]`).
  return ['all', existing, allow] as FilterSpecification
}
