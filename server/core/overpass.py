"""Overpass API client and element selection for click resolution.

Pure logic, no Django imports: everything here works on plain dicts as
returned by the Overpass JSON API.
"""

import math
import re
import time

import requests

# Tried in order. Vetted 2026-07-15: the list had held three mirrors that
# were entirely dead — kumi.systems and private.coffee never answered at
# all (not even /api/status), and osm.jp serves a TLS cert for another
# host. The fallback they promised was fiction, and each failing pass paid
# ~30s of connect timeouts to learn nothing, so a doomed resolution took
# ~2min to report failure. Retrying the one live instance is what actually
# recovers a busy 504, and it usually does so within seconds.
#
# Before adding a mirror, check it returns *data* for a known feature, not
# merely HTTP 200. Regional extracts (overpass.osm.ch, say, is Switzerland
# only) answer 200 with zero elements for everywhere else — which resolve()
# cannot distinguish from "no such feature here", so it would silently
# create bogus level-3 name anchors for most of the planet.
#
# Real redundancy means self-hosting Overpass, a later concern.
OVERPASS_URLS = [
    'https://overpass-api.de/api/interpreter',
]
TIMEOUT_S = 15
# A cache-missing click hits Overpass live while the user waits, and a
# rejection is almost always a transient "no free slot" (429/504) that
# clears in seconds — so retry, backing off, before giving up. The budget
# is bounded by what a user will sit through rather than by hope: four
# passes at ~8s a rejection plus the backoffs is ~40s worst case.
RETRY_BACKOFFS_S = (1, 3, 6)
# overpass-api.de 406es generic client user agents; identify ourselves.
USER_AGENT = 'toponymia/0.1 (dherron@mailbox.org)'

# Click radius: ~20px at the given zoom/latitude, clamped so low zooms
# don't sweep in half a country and high zooms still catch label offsets.
MIN_RADIUS_M = 50
MAX_RADIUS_M = 10_000

# Same-name component walk (roads): OSM splits a road into a new way at
# every tag change, so one boulevard is easily 100+ ways. Ways within this
# distance of each other count as connected — strict node-sharing would
# miss dual carriageways, whose parallel one-way halves never share a node.
COMPONENT_JOIN_M = 30
COMPONENT_MAX_LOOPS = 100
COMPONENT_TIMEOUT_S = 30

# Relation geometry (line-like relations only — see LINEAR_RELATION_TYPES).
# The Mississippi's relation is 1064 member ways / ~19k vertices / 1.1 MB,
# fetched in ~5s: affordable but not free, so it happens once per relation
# resolve and is cached thereafter in Place.geometry.
RELATION_TIMEOUT_S = 60
# Members that aren't the feature itself. A waterway relation carries its
# side channels and tributaries alongside the main stem (the Mississippi:
# 907 main_stream ways, 155 side_stream, 2 tributary) — including them
# would drag the merged course, and the midpoint derived from it, off the
# river. `inner` is the multipolygon hole equivalent.
EXCLUDED_MEMBER_ROLES = frozenset({'side_stream', 'tributary', 'inner'})
# Only relations whose geometry is a *line* get a geometry + snapped label
# point. A boundary/multipolygon relation merges into a closed ring, and
# the midpoint of a ring sits on the city limits rather than downtown —
# for those the creating click (a city's P625, or where a user clicked)
# is already the better answer.
LINEAR_RELATION_TYPES = frozenset({'waterway', 'route'})

# Administrative levels that name a *city*, for the slug qualifier rung
# below Natural Earth's subdivision. The band is what excludes both ends:
# 2 and 4 are the country and the state NE already gives us, and 10 is the
# neighbourhood — `main-street-downtown` names nothing a reader can place.
#
# The upper bound is 8 because that is the city almost everywhere checked,
# and the *max* within the band is taken rather than a fixed level because
# Scotland has no level 8 at all: City of Edinburgh is 6, the same rung as
# Lincolnshire in England. Taking the most local thing in the band absorbs
# that variation without a per-country table.
CITY_ADMIN_LEVELS = frozenset({5, 6, 7, 8})

QID_RE = re.compile(r'^Q\d+$')

_TYPE_RANK = {'relation': 0, 'way': 1, 'node': 2}
# Relation `type` tags that describe cartography rather than the place.
# `land_area` is the big one: OSM carries relation 11980 "France (terres)"
# — the *land mass* of France — tagged `wikidata=Q142`, exactly like the
# real boundary relation 2202162. It has a lower id, so it used to win the
# tiebreak and Q142 resolved to a place titled "France (land mass)".
_DEPRIORITISED_RELATION_TYPES = frozenset({'land_area'})

# Populated places, as opposed to the administrative areas that contain
# them. Read as OSM `place=*` values *and* as click classes: `kindOf()`
# reports the tile `place` layer's own class, which uses the same words, so
# one set serves both sides of the comparison in `choose_element`.
#
# Deliberately excludes `state`/`province`/`county`/`district`/`region`/
# `country`, which is the entire point of the set — see `_settlement_qid`.
SETTLEMENT_PLACES = frozenset({
    'city',
    'town',
    'village',
    'hamlet',
    'borough',
    'suburb',
    'quarter',
    'neighbourhood',
    'municipality',
    'locality',
    'isolated_dwelling',
    'city_block',
})


class OverpassError(Exception):
    """Overpass was unreachable or returned an unusable response."""


def radius_for_click(zoom, lat):
    if zoom is None:
        return 500
    meters_per_px = 156543.03 * math.cos(math.radians(lat)) / 2 ** zoom
    return int(min(max(meters_per_px * 20, MIN_RADIUS_M), MAX_RADIUS_M))


def _escape(value):
    return value.replace('\\', '\\\\').replace('"', '\\"')


def fetch_elements(name, lat, lon, radius):
    """All named OSM elements matching `name` around the click point,
    followed by the administrative areas that contain that point.

    Uses `out bb` so ways/relations come back with a bounding box but not
    their (potentially huge) full geometry. (`center` can't be combined
    with `bb`; center_of derives a point from the bounds instead.)

    The trailing `is_in` supplies the **city rung** for slug qualifiers
    (`main-street-portland`), which the Natural Earth table cannot: NE
    admin-1 is a single tier — states, provinces, counties — with nothing
    below it, so every Main Street in Oregon computes the same qualifier.
    It rides along in this same request, so it costs **no extra call and
    adds no new failure mode**: a clicked mint already requires this call
    to have succeeded, and a rejection means there is no Place to slug at
    all.

    `area.a[boundary=administrative]` filters **server-side**, and that is
    load-bearing rather than tidiness. Splitting the merged response on
    `type == 'area'` in Python is not enough, because `is_in` also emits
    plain ways: at a tidal point in Boston it returned an untagged
    `natural=water` way that the `nwr` query itself does not match, and
    which `choose_element` would happily have anchored a High Street to.
    The filter also drops the junk `is_in` returns alongside the real
    areas — timezones, ceremonial and traditional counties, fire and
    weather zones, "Greater Lincolnshire" as a statistical region.
    """
    query = (
        '[out:json][timeout:10];'
        f'nwr["name"="{_escape(name)}"](around:{radius},{lat},{lon});'
        'out tags bb;'
        f'is_in({lat},{lon})->.a;'
        'area.a[boundary=administrative]->.b;'
        '.b out tags;'
    )
    return _call(query)


def split_areas(elements):
    """(features, containing_areas) from one fetch_elements response.

    Callers that only want anchor candidates must use this rather than
    passing the raw list to `choose_element`, which has no notion of an
    area and would rank one as a candidate like any other.
    """
    features, areas = [], []
    for element in elements:
        (areas if element.get('type') == 'area' else features).append(element)
    return features, areas


def city_name(areas):
    """Name of the most local city-scale area in `areas`, or None.

    "City-scale" is CITY_ADMIN_LEVELS, and the most local is the *highest*
    level in that band — Portland (8) over Multnomah County (6), with
    Downtown (10) out of range. Verified against real `is_in` responses
    2026-08-18: Portland 8, Boston 8, Lincoln 8, City of Edinburgh 6.

    Prefers `name:en`, which is absent far more often than not (Lincoln,
    Lincolnshire and Multnomah County all carry only `name`) but matters
    where it exists: Scotland's level-4 name is `Alba / Scotland`.
    """
    best = None
    for area in areas:
        tags = area.get('tags', {})
        # `boundary` is re-checked here even though fetch_elements already
        # filters on it server-side, because an admin_level alone does not
        # mean administrative: Edinburgh's `is_in` carries 'East Central
        # Scotland', a level-6 *statistical* region that would otherwise
        # outrank the city. Defence in depth, since the cost of the query
        # drifting is a permanent slug naming a region nobody lives in.
        if tags.get('boundary') != 'administrative':
            continue
        try:
            level = int(tags.get('admin_level'))
        except (TypeError, ValueError):
            continue
        if level not in CITY_ADMIN_LEVELS:
            continue
        name = tags.get('name:en') or tags.get('name')
        if name and (best is None or level > best[0]):
            best = (level, name)
    return best[1] if best else None


def fetch_by_qid(qid):
    """All OSM elements tagged with `qid`, **worldwide**.

    For callers that already know the entity (a seeding bot working from
    Wikidata, or a tile feature carrying a wikidata tag), this replaces
    the name-filtered guess with an exact match. Wikidata's P625 point and
    OSM's `name` tag disagree often enough — suffixes (深圳 vs 深圳市),
    missing native labels, and node placements a kilometre off centre —
    that a name+radius lookup misses entities it should find.

    **No proximity filter** (dropped 2026-07-21; it was 50 km). A QID is
    globally unique, so a radius can only ever produce false negatives —
    and it did, for exactly the features whose extent is largest.
    Overpass's `around` treats a relation as near a point only if its
    *members* are, and a country's borders are nowhere near its interior:
    `["wikidata"="Q30"](around:50000,39.8,-98.5)` — the geographic centre
    of the United States — returns **zero elements**, so the hint missed,
    the name query missed too, and the US fell all the way to a level-3
    name anchor. Widening is not the fix: a 1,500 km radius times out,
    while the unfiltered query answers in ~1.4 s because the wikidata tag
    is indexed.

    The trust model this changes: the principle that "a stale hint costs a
    query, not a resolution" now holds only for a QID **OSM doesn't carry
    at all**. A
    QID that exists but isn't the caller's entity resolves confidently to
    the wrong place instead of falling through. Accepted because in both
    real callers the QID is authoritative rather than a guess — topobot
    passes the QID of the article it is writing, and a tile feature's
    wikidata tag is on the very feature that was clicked. `choose_element`
    still prefers a relation, which is what keeps a stray mistagged node
    from winning (OSM has one tagged `wikidata=Q30`: a US consulate).
    """
    query = (
        '[out:json][timeout:25];'
        f'nwr["wikidata"="{_escape(qid)}"];'
        'out tags bb;'
    )
    return _call(query)


def fetch_way_geometry(way_id):
    """Coordinate list [(lon, lat), ...] for a way, or None."""
    query = f'[out:json][timeout:10];way({way_id});out geom;'
    for element in _call(query):
        geometry = element.get('geometry')
        if geometry:
            return [(point['lon'], point['lat']) for point in geometry]
    return None


def is_linear_relation(element):
    """True if this relation's geometry is a line rather than an area."""
    return (
        element['type'] == 'relation'
        and element.get('tags', {}).get('type') in LINEAR_RELATION_TYPES
    )


def fetch_relation_member_ways(relation_id):
    """Member ways of a relation, with geometry, minus the roles that
    aren't the feature itself (EXCLUDED_MEMBER_ROLES).

    Shaped like fetch_way_component's output — tags, bounds and a
    coordinate list per way — so both feed the same geometry builder.
    """
    query = (
        f'[out:json][timeout:{RELATION_TIMEOUT_S}];'
        f'rel({relation_id});'
        'out geom;'
    )
    ways = []
    for element in _call(query, timeout_s=RELATION_TIMEOUT_S + 5):
        for member in element.get('members', []):
            if member.get('type') != 'way' or not member.get('geometry'):
                continue
            if member.get('role', '') in EXCLUDED_MEMBER_ROLES:
                continue
            ways.append({
                'type': 'way',
                'id': member['ref'],
                'tags': {},
                'geometry': member['geometry'],
                'bounds': _bounds_of_points(member['geometry']),
            })
    return ways


def _bounds_of_points(points):
    lons = [p['lon'] for p in points]
    lats = [p['lat'] for p in points]
    return {
        'minlon': min(lons), 'minlat': min(lats),
        'maxlon': max(lons), 'maxlat': max(lats),
    }


def fetch_way_component(way_id, name):
    """All ways connected to way_id (within COMPONENT_JOIN_M) sharing
    `name`, each with tags, bounds, and geometry.

    One round trip: Overpass's `complete` statement loops the sub-query
    to a fixed point, walking the whole road server-side (verified: the
    131 ways of SW Barbur Boulevard, ~7 km, in one ~6-11 s call).
    """
    query = (
        '[out:json][timeout:25];'
        f'way({way_id});'
        f'complete({COMPONENT_MAX_LOOPS})'
        f'{{ way(around:{COMPONENT_JOIN_M})["name"="{_escape(name)}"]; }};'
        'out geom;'
    )
    return _call(query, timeout_s=COMPONENT_TIMEOUT_S)


def _call(query, timeout_s=TIMEOUT_S):
    error = None
    for pause in (0, *RETRY_BACKOFFS_S):
        if pause:
            time.sleep(pause)
        for url in OVERPASS_URLS:
            try:
                response = requests.post(
                    url,
                    data={'data': query},
                    headers={'User-Agent': USER_AGENT},
                    timeout=timeout_s,
                )
                response.raise_for_status()
                return response.json().get('elements', [])
            except (requests.RequestException, ValueError) as exc:
                error = exc
    raise OverpassError(str(error)) from error


def _is_admin_area(element):
    """True if this element is an administrative area rather than a place.

    Both spellings, because either alone misses real data: the tag on the
    relation is `boundary=administrative`, but some are typed only through
    `type=boundary`.
    """
    tags = element.get('tags', {})
    return (
        tags.get('boundary') == 'administrative'
        or tags.get('type') == 'boundary'
    )


def _settlement_qid(elements, feature_class):
    """QID of the settlement *itself* among these candidates, or None.

    A settlement and the administrative area named after it are two
    entities that share a name and, often, a footprint — and a name+radius
    query cannot tell them apart, because it returns both. Havana: OSM has
    node 26396457 (`place=city`, `wikidata=Q1563`, the city) and relation
    1854615 (`boundary=administrative`, `admin_level=4`,
    `wikidata=Q12588`, **Havana Province**, which also carries
    `place=city` because it is tagged for the capital it contains). Under
    the type rank alone the relation won every time, so clicking the
    city's own label wrote the province's article — and clicking Panama
    City resolved to relation 287668, the *country* of Panama.

    So when the click was on a settlement, find the element that is a
    settlement rather than an administrative area, and let its QID say
    which entity was asked for. `choose_element` then drops any candidate
    carrying a *different* QID: same QID means the boundary relation is
    that same city (Paris, Berlin, Wien, Buenos Aires — all still resolve
    to the relation, which has the better geometry), a different QID means
    a different place that merely shares the name.

    Gated on `feature_class` in both directions, which is what keeps this
    from inverting the bug or leaking into other categories:

    - Only a settlement click seeds. Clicking the *province* label sends
      `state`, no seed is taken, and the province still resolves to
      Q12588 as it should.
    - Only a settlement element seeds. Without that, a river clicked
      beside a same-named town would be demoted in favour of the town.

    A settlement with no `wikidata` tag yields no seed and so no change —
    this sharpens the ladder where OSM gives us the evidence and leaves it
    alone where it doesn't.
    """
    if feature_class not in SETTLEMENT_PLACES:
        return None
    seeds = [
        element for element in elements
        if element.get('tags', {}).get('place') in SETTLEMENT_PLACES
        and not _is_admin_area(element)
        and qid_of(element)
    ]
    if not seeds:
        return None
    return qid_of(min(seeds, key=lambda e: _TYPE_RANK.get(e['type'], 3)))


def choose_element(elements, feature_class=None):
    """Pick the best anchor candidate per the resolution ladder.

    Candidates that are a *different entity* from the settlement clicked
    go last (`_settlement_qid`). Then elements carrying a wikidata QID win
    (they anchor at level 1), then relations over ways over nodes ("prefer
    the relation that ways belong to"), then the place itself over a
    cartographic stand-in for it (`_DEPRIORITISED_RELATION_TYPES`).

    Beyond that the order is Overpass's own, which is by id — so a tie is
    broken by whichever element was mapped first. That is arbitrary but
    stable; don't read meaning into it.

    `feature_class` is the category the caller clicked, in `kindOf()`'s
    vocabulary. Omitted, the entity rung is skipped and the rest of the
    ladder is unchanged.
    """
    if not elements:
        return None

    settlement_qid = _settlement_qid(elements, feature_class)

    def sort_key(element):
        qid = qid_of(element)
        relation_type = element.get('tags', {}).get('type', '')
        return (
            1 if settlement_qid and qid and qid != settlement_qid else 0,
            0 if qid is not None else 1,
            _TYPE_RANK.get(element['type'], 3),
            1 if relation_type in _DEPRIORITISED_RELATION_TYPES else 0,
        )

    return min(elements, key=sort_key)


def qid_of(element):
    qid = element.get('tags', {}).get('wikidata', '')
    return qid if QID_RE.match(qid) else None


def center_of(element):
    """(lon, lat) for any element type, or None."""
    if element['type'] == 'node':
        return (element['lon'], element['lat'])
    center = element.get('center')
    if center:
        return (center['lon'], center['lat'])
    bounds = bounds_of(element)
    if bounds:
        return ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
    return None


# A stored bbox is a planar rectangle, so it can only describe an extent
# that doesn't cross the antimeridian. Anything wider than half the globe
# went the wrong way round; see bounds_of.
MAX_BBOX_LON_SPAN_DEG = 180


def bounds_of(element):
    """(min_lon, min_lat, max_lon, max_lat), or None when unusable.

    **None for an antimeridian-crossing extent**, which no planar
    rectangle can represent. Overpass signals the crossing by *wrapping*
    — `minlon > maxlon`, meaning "east from minlon, round the globe, to
    maxlon" — and countries with overseas territories are full of them:

    - France (relation 2202162) reports `minlon 0.0002 / maxlon -0.0013`,
      because its territories between them cover nearly every longitude.
    - The United States (148838) reports `minlon 144.41` (Guam) /
      `maxlon -64.36` (Maine): 151° across the Pacific.

    Taking those verbatim was a real bug. `Polygon.from_bbox` silently
    normalises inverted corners by swapping them, which yields the
    extent's **complement** — the US box became 209° wide across the
    Atlantic, Europe and Asia, excluding Hawaii and most of Alaska, while
    France collapsed to a 0.002°-wide ribbon down the prime meridian
    101° tall. Both then framed the whole world on "zoom to place", and
    both silently degraded the highlight viewport test and the resolve
    proximity cache, which OR in the bbox.

    Returning None is deliberate over storing the true wrapped extent:
    France really does span ~360°, so the honest rectangle frames the
    whole planet — correct and useless. With no bbox the caller falls
    back to `label_point`, which is *on* the feature, and the client
    picks a zoom from the feature class (`flyZoomFor` in MapView). The
    better answer for a country — frame its largest component — needs
    the area-relation member geometry that M4 deferred.

    Same hazard, and the same fix, as the M4 finding on the *incoming*
    viewport bbox in the highlights query; that one was handled and this
    one was not.
    """
    bounds = element.get('bounds')
    if not bounds:
        return None
    min_lon, max_lon = bounds['minlon'], bounds['maxlon']
    # Equal is fine: a due-north-south way is legitimately zero-wide.
    if max_lon < min_lon:
        return None
    if max_lon - min_lon > MAX_BBOX_LON_SPAN_DEG:
        return None
    return (min_lon, bounds['minlat'], max_lon, bounds['maxlat'])
