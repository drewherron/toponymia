"""Overpass API client and element selection for click resolution.

Pure logic, no Django imports: everything here works on plain dicts as
returned by the Overpass JSON API.
"""

import contextlib
import contextvars
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
# rejection is often a transient "no free slot" (504) that clears in
# seconds — so retry, backing off, before giving up.
#
# **These are per-call and they are not the real limit.** The ceiling that
# matters is the request-wide budget below, because the ladder used to be
# sized against user patience alone and that turned out to be a fiction:
# gunicorn kills a worker at `--timeout`, so four passes at 15 s each plus
# 10 s of backoff — 70 s — never completed. It was shot at 30 s, five times
# on 2026-08-19 alone, and each kill is a 502 for whoever clicked.
RETRY_BACKOFFS_S = (1, 3, 6)

# 429 is not 504 and must not be retried like one. A "no free slot" clears
# on its own; a rate limit is Overpass telling us to stop, and asking again
# a second later is what deepens it — `overpass-api.de` refused three
# consecutive *trivial* queries after roughly six requests in a session
# [observed 2026-08-17]. So: one retry, after a pause long enough to be
# worth making, and only if the request's budget still allows it.
RATE_LIMIT_BACKOFF_S = 5
RATE_LIMIT_RETRIES = 1
# overpass-api.de 406es generic client user agents; identify ourselves.
USER_AGENT = 'toponymia/0.1 (dherron@mailbox.org)'

# The request-wide Overpass budget, in seconds, or None for "no limit".
#
# Set per web request (core.views.resolve) and deliberately *not* set for
# management commands and other batch callers, which can afford to wait and
# have no worker being killed out from under them.
#
# It exists because per-call limits cannot bound a *request*: one resolve
# chains several calls, and a road that needed elements + component +
# geometry + containing areas could spend minutes between them while
# gunicorn's patience ran out at 30 s. A deadline shared by every call is
# the only thing that adds up correctly.
_deadline = contextvars.ContextVar('overpass_deadline', default=None)


@contextlib.contextmanager
def budget(seconds):
    """Give every Overpass call inside this block one shared deadline.

    Calls that are merely *enriching* — the component walk, relation
    geometry, containing areas — already degrade when Overpass fails, so
    running out of budget costs detail rather than the resolution. The two
    that decide identity, `fetch_elements` and `fetch_by_qid`, run first
    and so get the budget while it is untouched.
    """
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def _seconds_left():
    """Budget remaining, or math.inf when no budget is in force."""
    deadline = _deadline.get()
    return math.inf if deadline is None else deadline - time.monotonic()


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

# Administrative levels that always name somewhere a reader can place, for
# the slug qualifier rung below Natural Earth's subdivision. 2 and 4 are the
# country and the state NE already gives us, so the band starts at 5.
#
# The *max* within the band is taken rather than a fixed level, because
# Scotland has no level 8 at all: City of Edinburgh is 6, the same rung as
# Lincolnshire in England.
LOCALITY_ADMIN_LEVELS = frozenset({5, 6, 7, 8})

# Below the band, a level is admitted only on evidence that it is a
# settlement rather than a piece of one.
#
# **This is the correction to the first cut of this rung**, which capped at
# 8 and claimed the max-in-band rule absorbed per-country variation without
# a table. Production disproved that on 2026-08-19: in rural England level 8
# is a *district* — West Lindsey holds ~50 villages — so four Church Lanes
# in four villages all took `church-lane-west-lindsey` and fell to the
# numeric tail the rung exists to prevent. The village is level 10, the
# civil parish, which the cap had excluded.
#
# What separates it from a neighbourhood is a tag, not a number. Ingham CP
# carries `designation=civil_parish`; Portland's level-10 "Downtown"
# carries no designation and no `place` at all [both verified against
# Overpass, 2026-08-19].
SETTLEMENT_DESIGNATIONS = frozenset({'civil_parish'})

# `place` values that name a settlement in its own right. Narrower than
# SETTLEMENT_PLACES on purpose: `suburb`, `quarter`, `neighbourhood` and
# `city_block` are parts of a town, and naming a street after one of them
# is the `main-street-downtown` outcome this guards against.
TOWN_PLACES = frozenset({
    'city', 'town', 'village', 'hamlet', 'municipality',
})

# Where `boundary=place` settlement polygons rank against admin levels.
#
# They exist because much of urban England is **unparished** — there is no
# civil parish to find, so the ladder falls to the level-8 district, and
# whether that reads correctly is an accident of naming. Stafford district
# is named after Stafford town, so `church-lane-stafford` is right.
# Erewash district is named after a river and contains Ilkeston, Long Eaton
# and Sandiacre, so `church-street-erewash` names nothing a reader can
# place. OSM says as much outright: the point sits in a polygon called
# `Erewash (unparished area)`.
#
# A `boundary=place` polygon is the town itself, by containment, and it
# arrives in the same free `is_in` response. Ranked at 9: **below** a civil
# parish, which is finer-grained and should keep winning, and **above** the
# level-8 district it exists to beat.
#
# **Partial by nature — do not expect it to close the gap.** Coverage is
# sparse [surveyed 2026-08-19, one Overpass query over the East Midlands
# and Lincolnshire]: 40 such polygons in the whole region, and of 19 towns
# sampled only Ilkeston, Long Eaton, Boston, Grantham and Spalding have
# one. Worksop, Retford, Gainsborough, Market Rasen, Louth, Skegness,
# Newark-on-Trent, Sleaford and Sandiacre have none and still take their
# district. All 40 are relations; none are closed ways, so nothing is
# gained by looking for those.
PLACE_BOUNDARY_RANK = 9

# `place` values accepted from a *node*, and where a node ranks.
#
# A node is the weakest evidence in this ladder and the only one that is not
# containment: it is a dot someone dropped, so membership is inferred from
# proximity rather than read off a boundary. It earns its place because
# containment has two failure modes a boundary cannot fix:
#
# - **Too coarse.** Unparished England falls back to a district that may be
#   named after a river — `church-street-erewash` for a street in Ilkeston.
# - **Confidently wrong.** A Romanian commune is an admin_level 8 area named
#   after its seat village and covering several others, so containment names
#   a settlement the street is genuinely not in — observed in production
#   2026-08-19, a street resolving to a town three towns away.
#
# So it ranks **above the admin districts** (5–8) it exists to beat, and
# **below both settlement boundaries** — a civil parish (10) and a
# `boundary=place` polygon (9) are real edges and always win. That ordering
# is what keeps the measured risk off the common path: the error rate below
# was measured on streets inside towns that *have* a polygon, which is
# exactly where this rung never fires.
#
# **Known cost, accepted deliberately (2026-08-19).** Against ground truth in
# five English towns, nearest-node named a locality genuinely inside the town
# 15.6% of the time and a settlement outside it 17.3% of the time — roughly
# one real error per real refinement. `village` is included anyway, on Drew's
# call: the alternative names a street after a district or a distant commune
# seat, and a nearby village is more often right than either. `hamlet` and
# `suburb` are excluded — the first is finer than the evidence supports, the
# second names part of a town.
NODE_PLACES = frozenset({'city', 'town', 'village'})
PLACE_NODE_RANK = 8.5
# A street sits a median 1.1–1.4 km from its own town's node, p90 ~2 km
# [measured 2026-08-19 over 2288 streets]. 3 km covers that with margin while
# staying far short of reaching the next town across open country.
PLACE_NODE_MAX_M = 3_000

# The two kinds of containing area worth keeping, as an Overpass union.
# Filtering server-side is load-bearing rather than tidy: `is_in` also
# emits plain ways (a tidal river at one Boston point), and it returns
# timezones, ceremonial and traditional counties, fire and weather zones
# alongside the real areas.
_AREA_UNION = (
    'area{sets}[boundary=administrative];'
    'area{sets}[boundary=place];'
)

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
        f'({_AREA_UNION.format(sets=".a")})->.b;'
        '.b out tags;'
    )
    return _call(query)


def fetch_common_areas(points):
    """Administrative areas containing **every** one of `points`.

    This is what matches the qualifier's scale to the feature's own
    extent. A street inside one village has all its probe points in that
    village, so the village survives the intersection and names the slug.
    A road running through three villages shares only the district with
    itself, so the district does — which is the right answer rather than a
    consolation, because no one village describes where that road is.
    The same mechanism handles a river across several counties, and a
    feature out in open country, whose points share only whatever wide
    area actually contains it.

    Overpass intersects the sets itself — `area.p0.p1.p2` is membership of
    all three — so however many points are probed this stays **one
    request**, and the response is already the answer rather than three
    sets to reconcile here.
    """
    if not points:
        return []
    statements = ''.join(
        f'is_in({point.y},{point.x})->.p{i};'
        for i, point in enumerate(points)
    )
    sets = ''.join(f'.p{i}' for i in range(len(points)))
    query = (
        f'[out:json][timeout:25];{statements}'
        f'({_AREA_UNION.format(sets=sets)})->.common;'
        '.common out tags;'
    )
    return [e for e in _call(query) if e.get('type') == 'area']


def fetch_place_nodes(points, radius_m=PLACE_NODE_MAX_M):
    """Settlement nodes within `radius_m` of **every** point.

    The node analogue of `fetch_common_areas`, and intersected for the same
    reason: a street inside one village has all its probe points near that
    village's node, while a road running through three has no single node
    near all of them and so declines to whatever contains the lot. Overpass
    does the intersection — chaining `around` onto a node *set* filters it
    rather than adding to it — so this stays one request.

    Deliberately a separate call rather than more statements on the
    `fetch_elements` query. Those results share one response with the
    feature candidates, and a `place` node arriving in that list would be
    offered to `choose_element` as an anchor — the same way an `is_in`
    tidal-river way once was. Keeping it separate makes that impossible,
    and the caller only spends the request when no settlement boundary
    already answered.
    """
    if not points:
        return []
    kinds = '|'.join(sorted(NODE_PLACES))
    lat, lon = points[0].y, points[0].x
    parts = [
        '[out:json][timeout:25];',
        f'node[place~"^({kinds})$"](around:{radius_m},{lat},{lon})->.n0;',
    ]
    for i, point in enumerate(points[1:], start=1):
        parts.append(
            f'node.n{i - 1}(around:{radius_m},{point.y},{point.x})->.n{i};'
        )
    parts.append(f'.n{len(points) - 1} out;')
    return [e for e in _call(''.join(parts)) if e.get('type') == 'node']


def nearest_place_node(nodes, lat, lon, max_m=PLACE_NODE_MAX_M):
    """(distance, name) of the closest usable settlement node, or None.

    Nearest wins outright, with no preference for the larger `place` type.
    Preferring a town over a village would reintroduce the Romanian case
    this rung exists for, where the correct answer is the village you are
    standing in and the wrong one is a town several villages away.
    """
    best = None
    for node in nodes:
        name = node.get('tags', {}).get('name:en') or \
            node.get('tags', {}).get('name')
        if not name or node['tags'].get('place') not in NODE_PLACES:
            continue
        if 'lat' not in node or 'lon' not in node:
            continue
        d = _haversine_m(lat, lon, node['lat'], node['lon'])
        if d <= max_m and (best is None or d < best[0]):
            best = (d, name)
    return best


def _haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    h = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2)
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(h)))


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


def locality_best(areas):
    """(rank, name) of the most local nameable area, or None.

    The rank is what lets a caller weigh containment against the node rung:
    a settlement boundary outranks a node, an admin district does not.
    """
    best = None
    for area in areas:
        rank = _locality_rank(area)
        if rank is None:
            continue
        tags = area.get('tags', {})
        name = tags.get('name:en') or tags.get('name')
        if name and (best is None or rank > best[0]):
            best = (rank, name)
    return best


def locality_name(areas):
    """Name of the most local nameable area in `areas`, or None.

    "Most local" is the highest `_locality_rank`, so the answer tracks
    whatever tier the country actually uses: Portland at 8, City of
    Edinburgh at 6 (Scotland has no 8), Ingham CP at 10, and Ilkeston via
    its settlement polygon where England is unparished.

    Prefers `name:en`, which is absent far more often than not (Lincoln,
    Lincolnshire and Multnomah County all carry only `name`) but matters
    where it exists: Scotland's level-4 name is `Alba / Scotland`.
    """
    best = locality_best(areas)
    return best[1] if best else None


def _locality_rank(area):
    """How local this area is, or None if its name may not stand in a slug.

    Higher is more local, and the scale is admin_level's, so the two kinds
    of area compare directly: a civil parish at 10 beats a settlement
    polygon at 9, which beats a district at 8.

    `boundary` decides which rules apply, and is checked here even though
    the queries already filter on it server-side. An admin_level alone does
    not mean administrative — Edinburgh's `is_in` carries 'East Central
    Scotland', a level-6 *statistical* region that would otherwise outrank
    the city — and the cost of a query drifting is a permanent slug naming
    a region nobody lives in.
    """
    tags = area.get('tags', {})
    boundary = tags.get('boundary')

    if boundary == 'place':
        # A settlement polygon is only ever the settlement, so its `place`
        # value is the whole test — and TOWN_PLACES is what keeps a
        # `place=suburb` polygon from naming a street after part of a town.
        return (
            PLACE_BOUNDARY_RANK if tags.get('place') in TOWN_PLACES else None
        )
    if boundary != 'administrative':
        return None

    try:
        level = int(tags.get('admin_level'))
    except (TypeError, ValueError):
        return None
    if level in LOCALITY_ADMIN_LEVELS:
        return level
    if level <= max(LOCALITY_ADMIN_LEVELS):
        # Country and state: NE already supplies these, and this rung
        # would only repeat them.
        return None
    # Below the band, only on evidence that this is a settlement rather
    # than a piece of one — an English civil parish, not an American
    # level-10 neighbourhood.
    if (tags.get('designation') in SETTLEMENT_DESIGNATIONS
            or tags.get('place') in TOWN_PLACES):
        return level
    return None


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


class _RateLimited(Exception):
    """Overpass answered 429. Distinct from every other failure because the
    right response is to back off hard, not to try again immediately."""


def _call(query, timeout_s=TIMEOUT_S):
    """Run `query`, retrying transient failures within the budget.

    Three things bound this, and the tightest wins:

    - `timeout_s`, how long one attempt may wait on the socket;
    - the retry ladder, how many attempts are worth making;
    - `_seconds_left()`, the request-wide deadline — which is what keeps
      the sum under gunicorn's `--timeout` instead of over it.

    Never sleeps past the deadline and never starts an attempt it cannot
    finish inside it, so a caller that runs out of budget fails *now* with
    an OverpassError rather than being killed mid-request.
    """
    error = None
    backoffs = iter(RETRY_BACKOFFS_S)
    rate_limit_retries = RATE_LIMIT_RETRIES
    pause = 0

    while True:
        left = _seconds_left()
        # No time to wait out the backoff and still make the request.
        if left <= 0 or pause >= left:
            break
        if pause:
            time.sleep(pause)
            left = _seconds_left()
            if left <= 0:
                break
        try:
            return _attempt(query, min(timeout_s, left))
        except _RateLimited as exc:
            error = exc
            if rate_limit_retries <= 0:
                break
            rate_limit_retries -= 1
            pause = RATE_LIMIT_BACKOFF_S
        except (requests.RequestException, ValueError) as exc:
            error = exc
            pause = next(backoffs, None)
            if pause is None:
                break

    raise OverpassError(str(error) if error else 'out of time') from error


def _attempt(query, timeout_s):
    """One pass over every mirror. Raises rather than returning on failure.

    A 429 does not short-circuit the pass: another mirror may well answer,
    and only when none of them does is this a rate limit worth backing off
    from. If mirrors disagree — one limiting, one merely busy — the limit
    wins, because the longer pause is the safe way to be wrong.
    """
    error = None
    rate_limited = False
    for url in OVERPASS_URLS:
        try:
            response = requests.post(
                url,
                data={'data': query},
                headers={'User-Agent': USER_AGENT},
                timeout=timeout_s,
            )
            # Checked before raise_for_status, which would flatten the 429
            # into an HTTPError indistinguishable from the 504 we *do*
            # want to retry hard.
            if response.status_code == 429:
                rate_limited = True
                continue
            response.raise_for_status()
            return response.json().get('elements', [])
        except (requests.RequestException, ValueError) as exc:
            error = exc
    if rate_limited:
        raise _RateLimited('every mirror rate limited')
    if error is not None:
        raise error
    raise OverpassError('no Overpass mirrors configured')


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
