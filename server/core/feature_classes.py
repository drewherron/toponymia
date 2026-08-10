"""Which `feature_class` values may create a Place.

The client keeps two allowlists in `web/src/poi.ts` — one for the map's
clickable features, one for geocoder hits — so that a restaurant called OJAI
cannot take the slug `ojai` from the town. Both are prevention in the UI:
`feature_class` arrives as a free-form string the client chose, so a direct
`POST /api/resolve/` bypassed the rule entirely. This module is the rule.

**A third vocabulary, not a copy of either.** `web/src/poi.ts` filters
OpenMapTiles' `class`/`subclass`; the Photon list filters raw OSM `key`/`value`.
The server sees neither. It sees whatever `kindOf()` (map clicks) or
`kindFromPhoton()` (search picks) reduced those to, so this list is written in
*that* vocabulary and has to be maintained against those two functions in
`web/src/map/features.ts` and `web/src/api.ts`.

**Stricter than search, deliberately.** The Photon rule passes any key it
hasn't heard of; an allowlist cannot. So a legitimate but unenumerated category
— `natural=fjord`, say — is rejected here until someone adds it below. That is
the intended direction of failure: a rejection is a visible 400 that gets
reported and fixed in one commit, while the alternative is a bad Place holding
a good slug permanently, which is the failure this project already had once.
The error names the offending class so the report is actionable.

**Enforced on creation only.** An existing Place is still returned whatever its
class, so removing an entry here never breaks an article that already exists.
"""


class DisallowedFeatureClass(ValueError):
    """Raised when a resolve request would mint a Place we don't want."""

    def __init__(self, feature_class):
        self.feature_class = feature_class
        super().__init__(
            f'{feature_class!r} is not a place category Toponymia creates '
            f'articles for'
        )


# Settlements and administrative units. `kindOf()` returns the `place` layer's
# own class for these, and `kindFromPhoton()` returns the raw `place=*` value,
# so the two vocabularies coincide here and this one list serves both.
SETTLEMENT_CLASSES = {
    'continent',
    'country',
    'state',
    'province',
    'region',
    'district',
    'county',
    'municipality',
    'city',
    'borough',
    'town',
    'village',
    'hamlet',
    'suburb',
    'quarter',
    'neighbourhood',
    'city_block',
    'locality',
    'isolated_dwelling',
    'farm',
    'allotments',
    'square',
    'place',
}

# Natural features. `place=island`/`islet`/`archipelago` land here rather than
# above because that is what they are, not because of which OSM key carries
# them — the set is keyed on meaning, and nothing downstream cares which
# constant a class came from.
NATURAL_CLASSES = {
    'island',
    'islet',
    'archipelago',
    'peak',
    'volcano',
    'ridge',
    'saddle',
    'valley',
    'glacier',
    'cliff',
    'cape',
    'bay',
    'strait',
    'fjord',
    'isthmus',
    'beach',
    'dune',
    'reef',
    'spring',
    'water',
    'waterway',
    'ocean',
    'sea',
    'wood',
    'forest',
    'heath',
    'moor',
    'scrub',
    'grassland',
    'wetland',
    'desert',
    'plateau',
    'hill',
}

# Human-made features that carry names of their own. `road` covers every
# `highway=*` pick because `kindFromPhoton()` collapses them, and `boundary`
# arrives from the boundary source layer. `castle`, `lighthouse`, `attraction`
# and `station` are the whole of what the `poi` layer can produce:
# `POI_CLASS_ALLOWLIST` in `web/src/poi.ts` keeps every other POI off the map,
# and the tile schema's `railway` is renamed `station` by `kindOf()` so a map
# click and a search pick on one station agree.
CONSTRUCTED_CLASSES = {
    'road',
    'boundary',
    'aerodrome',
    'aeroway',
    'station',
    'castle',
    'lighthouse',
    'attraction',
    'monument',
    'memorial',
    'ruins',
    'park',
    'national_park',
    'protected_area',
    'nature_reserve',
    'aboriginal_lands',
    'garden',
    'common',
    'meadow',
    'reservoir',
    'dam',
    'bridge',
    'tunnel',
}

ALLOWED_FEATURE_CLASSES = (
    SETTLEMENT_CLASSES | NATURAL_CLASSES | CONSTRUCTED_CLASSES
)


def check_allowed(feature_class):
    """Raise DisallowedFeatureClass unless this class may create a Place."""
    if feature_class not in ALLOWED_FEATURE_CLASSES:
        raise DisallowedFeatureClass(feature_class)
