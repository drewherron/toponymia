"""Server-side click resolution: name+point -> anchored Place.

Ladder: cached Place -> Overpass around-query ->
wikidata QID (level 1) -> OSM element (level 2) -> name+location (level 3).
"""

import math
from collections import Counter

from django.contrib.gis.geos import (
    LineString,
    MultiLineString,
    Point,
    Polygon,
)
from django.contrib.gis.measure import D
from django.db.models import Q

from . import feature_classes, overpass
from .admin_areas import (
    admin_qualifier,
    locality_qualifier,
    nearest_admin_area,
)
from .models import Place
from .slugs import unique_slug

# Below this end-to-end extent, a feature is small enough that the point
# someone clicked already answers what contains it, so the mint spends no
# extra Overpass request to ask again about three points that would all
# land in the same village. Above it, the feature may span more than one
# area and the intersection is worth a request.
PROBE_MIN_EXTENT_M = 1_000

# How far label_point may sit from the click and still let the *click's*
# areas qualify the slug. Only guards the small-feature path above, where
# the areas come from the click rather than from the feature's own course.
CLICK_AREA_MAX_OFFSET_M = 25_000


def resolve(name, feature_class, lng, lat, zoom=None, name_en=None,
            qid=None, allow_create=True):
    """Return (place, created). Raises overpass.OverpassError on outage,
    or feature_classes.DisallowedFeatureClass when a request that would
    create a row names a category this wiki doesn't write articles about.

    `name` is the feature's native OSM name (what Overpass matches on);
    `name_en` is the English-first label the client displayed, preferred
    for display_name so the article is titled what the user clicked.

    `qid` is an optional Wikidata hint from a caller that already knows
    the entity. It short-circuits the name guess: an existing Place with
    that QID wins outright, else Overpass is queried by wikidata tag,
    which anchors at level 1 by construction. A miss falls through to the
    name ladder below, so a wrong or stale hint costs a query, not a
    resolution.

    `allow_create=False` restricts this to the database rungs of the
    ladder: an already-known Place is returned as usual, and anything that
    would query Overpass or write a new row returns (None, False) instead.
    That is the anonymous path — it keeps clicking a known place instant and
    free while making sure only accounts can spend our Overpass budget or
    create permanent rows.
    """
    radius = overpass.radius_for_click(zoom, lat)
    click = Point(lng, lat, srid=4326)

    if qid:
        existing = Place.objects.filter(wikidata_qid=qid).first()
        if existing:
            return existing, False

    display_names = {name, name_en} - {None}
    # bbox matters for relations, which cache no geometry: without it a
    # low-zoom click far from the centroid re-creates the place.
    cached = (
        Place.objects.filter(
            display_name__in=display_names, feature_class=feature_class
        )
        .filter(
            Q(centroid__dwithin=(click, D(m=radius)))
            | Q(label_point__dwithin=(click, D(m=radius)))
            | Q(geometry__dwithin=(click, D(m=radius)))
            | Q(bbox__dwithin=(click, D(m=radius)))
        )
        .first()
    )
    if cached:
        return cached, False

    # Past the cache, so this request would mint a row. The category rule
    # applies from here and not above it: an existing Place resolves whatever
    # its class, so retiring a category never breaks an article already
    # written. Checked ahead of the anonymous gate below so a bad class gets
    # the same 400 whoever sends it, rather than a 401 that hides it behind a
    # sign-in prompt.
    feature_classes.check_allowed(feature_class)

    # Everything below here either calls Overpass or creates a row, so this
    # is where the anonymous path stops.
    if not allow_create:
        return None, False

    element = None
    # Containing administrative areas, for the locality rung of the slug
    # qualifier. Only the name query carries them: `fetch_by_qid` is a
    # worldwide lookup whose element can be nowhere near this click, so
    # anything derived from the click would name the wrong town outright.
    areas = []
    if qid:
        element = overpass.choose_element(
            overpass.fetch_by_qid(qid), feature_class
        )
    if element is None:
        features, areas = overpass.split_areas(
            overpass.fetch_elements(name, lat, lng, radius)
        )
        element = overpass.choose_element(features, feature_class)
    if element is None:
        return (
            _create_name_anchor(name_en or name, feature_class, click, areas),
            True,
        )

    # Roads: OSM splits a road into a way per tag change, so anchor and
    # geometry come from the whole same-name connected component — one
    # article per road, and highlights that light up along its length.
    component = _component_for(element, name)
    # The anchor is the component's lowest way id, so clicks on any two
    # segments resolve deterministically to the same place.
    anchor_id = min(w['id'] for w in component) if component else element['id']
    # A sibling segment often carries the road's wikidata tag when the
    # clicked one doesn't.
    qid = overpass.qid_of(element) or _component_qid(component) or qid

    if qid:
        existing = Place.objects.filter(wikidata_qid=qid).first()
        if existing:
            return existing, False
    else:
        existing = Place.objects.filter(
            osm_type=element['type'], osm_id=anchor_id
        ).first()
        if existing:
            return existing, False

    return (
        _create_from_element(
            element, qid, name, feature_class, click, name_en,
            component=component, anchor_id=anchor_id, areas=areas,
        ),
        True,
    )


def _metres_between(a, b):
    """Great-circle distance in metres between two Points.

    Haversine rather than a GEOS call: both points are lon/lat in 4326,
    where `.distance()` returns degrees, and the only question asked of
    the answer is a coarse "same city or not" threshold.
    """
    if a is None or b is None:
        return float('inf')
    lon1, lat1, lon2, lat2 = map(
        math.radians, (a.x, a.y, b.x, b.y)
    )
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(h)))


def _areas_containing_all_of(geometry, click, label_point, click_areas):
    """Administrative areas that contain the *whole* feature.

    This is what keeps the qualifier's scale honest. Asking `is_in` about
    one point says only where that point is, and for anything longer than
    a village street that is a coin toss between the areas it passes
    through. Asking about the start, middle and end and keeping what they
    share says how far the feature actually reaches, so the name that ends
    up in the slug is one that describes all of it.

    Two shortcuts, both of which return the click's own areas — free,
    since they arrived with the mint's first request:

    - **No line to walk** (a node, an area relation, a failed geometry
      fetch). There is nothing to span, so one point is the whole story.
    - **A feature under PROBE_MIN_EXTENT_M end to end.** Its three probe
      points would land in the same place the click did, so the request
      would buy nothing.

    Never raises. A qualifier is a nicety and a mint is not: an Overpass
    failure here falls back to the click's areas, and the ladder below
    still has Natural Earth and the numeric floor under it.
    """
    points = probe_points(geometry)
    if points is None or _spread_m(points) < PROBE_MIN_EXTENT_M:
        # The click's areas describe the click. That is the same place as
        # the feature for a small one, but on the fetch_by_qid path the
        # element can be a continent away, so check before trusting it.
        if _metres_between(click, label_point) > CLICK_AREA_MAX_OFFSET_M:
            return []
        return click_areas or []
    try:
        return overpass.fetch_common_areas(points)
    except overpass.OverpassError:
        return []


def _spread_m(points):
    """Greatest distance between any two of `points`."""
    return max(
        (_metres_between(a, b) for i, a in enumerate(points)
         for b in points[i + 1:]),
        default=0.0,
    )


def _component_for(element, name):
    """Same-name connected ways for a way anchor, or None.

    Never raises: a failed walk degrades to the single-way behavior
    (anchor to the clicked segment, cache its geometry alone).
    """
    if element['type'] != 'way':
        return None
    way_name = element.get('tags', {}).get('name') or name
    try:
        return overpass.fetch_way_component(element['id'], way_name) or None
    except overpass.OverpassError:
        return None


def _component_qid(component):
    """Most common valid QID among component members, or None."""
    qids = [q for q in (overpass.qid_of(w) for w in component or []) if q]
    return Counter(qids).most_common(1)[0][0] if qids else None


def _create_from_element(element, qid, name, feature_class, click,
                         name_en=None, component=None, anchor_id=None,
                         areas=None):
    tags = element.get('tags', {})
    display_name = (
        tags.get('name:en') or name_en or tags.get('name') or name
    )
    bounds = overpass.bounds_of(element)

    geometry = None
    if element['type'] == 'way':
        if component:
            geometry = _component_geometry(component)
            bounds = _component_bounds(component) or bounds
        if geometry is None:
            coords = overpass.fetch_way_geometry(element['id'])
            if coords and len(coords) >= 2:
                geometry = LineString(coords, srid=4326)
    elif overpass.is_linear_relation(element):
        # Line-like relations (rivers, routes) cache their course, so the
        # label point can be snapped onto it. Area relations still keep
        # centroid+bbox only — a ring's midpoint is on the city limits.
        geometry = _relation_geometry(element['id'])
    # area relations: centroid+bbox only (full geometry deferred to M4)

    center = overpass.center_of(element)
    if component and bounds:
        # center of the whole component, not the clicked segment
        center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
    centroid = Point(*center, srid=4326) if center else click
    if element['type'] == 'node':
        geometry = centroid

    bbox = Polygon.from_bbox(bounds) if bounds else None

    # A node IS the feature. Otherwise prefer a point snapped to the middle
    # of the course; failing that (no geometry — area relations, a failed
    # geometry fetch) the click is still the only point known to lie on the
    # feature, since bbox centroids can sit far off it.
    #
    # Snap to the FULL course, then thin for storage — not the reverse. The
    # dot is drawn over the basemap, which renders OSM's true geometry, so
    # the point has to lie on *that*. Snapping to our thinned copy instead
    # would put it up to a tolerance (500 m on the Mississippi) off the
    # river the user can see. The cost is that the stored geometry no
    # longer passes exactly through label_point — by 130 m here, and
    # nothing draws the stored geometry at a zoom where that resolves.
    label_point = (
        centroid if element['type'] == 'node'
        else representative_point(geometry) or click
    )
    geometry = simplified(geometry)

    # Qualify from label_point, never the click: on the fetch_by_qid path
    # the Overpass query is worldwide, so the element can be nowhere near
    # where the user clicked, and a click-derived qualifier would name the
    # wrong country outright. label_point is guaranteed to lie on the
    # feature. For a river it is also mid-course and therefore
    # deterministic, so a linear feature's qualifier no longer depends on
    # which segment someone happened to hit.
    area = nearest_admin_area(label_point)

    locality = locality_qualifier(
        overpass.locality_name(
            _areas_containing_all_of(geometry, click, label_point, areas)
        ),
        display_name,
        area,
    )

    return Place.objects.create(
        slug=unique_slug(
            display_name,
            admin_qualifier(area, display_name, qid, feature_class),
            locality=locality,
        ),
        **_admin_context(area),
        wikidata_qid=qid,
        osm_type=element['type'],
        osm_id=anchor_id if anchor_id is not None else element['id'],
        anchor_level=(
            Place.AnchorLevel.WIKIDATA if qid else Place.AnchorLevel.OSM
        ),
        display_name=display_name,
        feature_class=feature_class,
        geometry=geometry,
        centroid=centroid,
        label_point=label_point,
        bbox=bbox,
    )


# Stored geometry is a display-and-filter approximation, not survey truth
# (see Place.geometry). The tolerance scales with each feature's own
# extent, so the worst-case deviation is a constant fraction of a pixel at
# the zoom that frames the whole feature — about viewport_px/DIVISOR, i.e.
# well under a pixel at 1600px wide. A fixed tolerance can't work for both
# ends of the range: 500 m is invisible on the Mississippi (18° across,
# ~850 m/px when framed) and 8 px of error on a creek.
# Measured: Mississippi 292 kB -> 47 kB, Columbia 110 kB -> 17 kB.
SIMPLIFY_EXTENT_DIVISOR = 4000

# How wide a break still counts as the same course, in degrees (~2 km).
# Sized off the real gaps in OSM relation data (the Mississippi's is
# 950 m) and kept well under the distance at which two genuinely
# different features would be mistaken for one.
STITCH_TOLERANCE_DEG = 0.02
# Guard against a pathological relation turning the O(n²) join quadratic
# on hundreds of pieces; GEOS normally leaves only a handful.
STITCH_MAX_CHAINS = 60


def simplified(geometry):
    """Drop vertices too fine to draw at the feature's own framing zoom.

    Take the label point *before* calling this — thinning removes meander
    length unevenly (the Mississippi loses 4.8% of its course), which
    moves the arc-length midpoint 63 km. See _create_from_element.
    """
    if geometry is None or geometry.geom_type == 'Point':
        return geometry
    min_x, min_y, max_x, max_y = geometry.extent
    span = max(max_x - min_x, max_y - min_y)
    if not span:
        return geometry
    thinned = geometry.simplify(
        span / SIMPLIFY_EXTENT_DIVISOR, preserve_topology=True
    )
    # GEOS collapses a one-part MultiLineString to a LineString. Callers
    # (and the stored rows) are entitled to get back the type they gave.
    if (
        geometry.geom_type == 'MultiLineString'
        and thinned.geom_type == 'LineString'
    ):
        thinned = MultiLineString([thinned], srid=geometry.srid)
    return thinned


def _path_length(coords):
    return sum(
        math.dist(a, b) for a, b in zip(coords, coords[1:], strict=False)
    )


def _stitch(lines, tolerance):
    """Join coordinate runs whose ends nearly meet. Returns a list of
    coordinate lists, longest-first ordering not guaranteed.

    Greedy and order-independent: repeatedly joins the first pair of runs
    within `tolerance` of each other, in whichever of the four
    end-to-end orientations is closest.
    """
    chains = [list(line.coords) for line in lines]
    if len(chains) > STITCH_MAX_CHAINS:
        return chains

    joined = True
    while joined and len(chains) > 1:
        joined = False
        for i in range(len(chains)):
            for j in range(i + 1, len(chains)):
                a, b = chains[i], chains[j]
                options = (
                    (math.dist(a[-1], b[0]), a + b),
                    (math.dist(a[-1], b[-1]), a + b[::-1]),
                    (math.dist(a[0], b[0]), a[::-1] + b),
                    (math.dist(a[0], b[-1]), b + a),
                )
                gap, combined = min(options, key=lambda option: option[0])
                if gap <= tolerance:
                    chains[i] = combined
                    chains.pop(j)
                    joined = True
                    break
            if joined:
                break
    return chains


def _relation_geometry(relation_id):
    """MultiLineString of a line-like relation's member ways, or None.

    Never raises: a failed fetch degrades to the old behavior (no cached
    geometry, label point falls back to the click).
    """
    try:
        members = overpass.fetch_relation_member_ways(relation_id)
    except overpass.OverpassError:
        return None
    return _component_geometry(members)


def representative_point(geometry):
    """A point that lies ON `geometry`, as near its middle as we can get.

    The bbox centroid of a long, curving feature is not on the feature at
    all — the Mississippi's sits ~170 km west of the river in Missouri,
    the Seine's ~30 km out in farmland — which is why label_point exists.
    But taking the creating click instead just moves the problem: it puts
    the dot wherever the first resolver happened to touch the feature,
    and for a bot seeding from Wikidata that is P625, i.e. a river's
    *source* (the Mississippi's dot sat in northern Minnesota).

    So: merge the segments into continuous chains, take the longest chain
    (branching and side channels drop away, leaving the main course), and
    walk half its length. For a river that lands mid-course by
    construction, however much it meanders.

    Interpolation is planar over lng/lat degrees, so "half the length" is
    slightly off from half the great-circle length — irrelevant for
    placing a dot, and it keeps this a pure GEOS call with no reprojection.
    """
    if geometry is None:
        return None
    if geometry.geom_type == 'Point':
        return geometry
    line = main_line(geometry)
    if line is not None:
        midpoint = line.interpolate_normalized(0.5)
        return Point(midpoint.x, midpoint.y, srid=4326)
    # Areas and anything else: the guaranteed-interior point.
    return geometry.point_on_surface


def main_line(geometry):
    """The feature's longest continuous course, or None if it has none.

    Split out of representative_point so that the slug qualifier can walk
    the same course the label point sits on — one definition of "the
    feature's line", used for both.
    """
    if geometry is None or geometry.geom_type == 'Point':
        return None

    line = geometry
    if geometry.geom_type == 'MultiLineString':
        # ST_LineMerge: stitches segments that share endpoints into runs.
        merged = geometry.merged
        if merged.geom_type == 'MultiLineString':
            # Still in pieces. LineMerge needs an exactly shared node, and
            # real data has gaps — the Mississippi splits into two runs
            # either side of a 950 m break near La Crosse, which would
            # leave "the longest run" meaning the lower 69% of the river
            # and put the midpoint ~350 km downstream of the true one. So
            # close small gaps first, then take the longest.
            chains = _stitch(merged, STITCH_TOLERANCE_DEG)
            longest = max(chains, key=_path_length)
            line = (
                LineString(longest, srid=4326) if len(longest) >= 2
                else merged[0]
            )
        else:
            line = merged
    if line.geom_type == 'LineString' and line.length:
        return line
    return None


def probe_points(geometry):
    """Start, middle and end of the feature's course — the points whose
    *shared* containing areas say how far the feature reaches.

    The middle is not redundant with the two ends. A ring road closes on
    itself, so its endpoints coincide and would together describe a
    single spot; the midpoint is what reveals that it encircles a town.
    """
    line = main_line(geometry)
    if line is None:
        return None
    points = []
    for fraction in (0.0, 0.5, 1.0):
        point = line.interpolate_normalized(fraction)
        points.append(Point(point.x, point.y, srid=4326))
    return points


def _component_geometry(component):
    """MultiLineString of every member way's geometry, or None."""
    lines = [
        LineString(
            [(p['lon'], p['lat']) for p in way['geometry']], srid=4326
        )
        for way in component
        if len(way.get('geometry') or []) >= 2
    ]
    return MultiLineString(lines, srid=4326) if lines else None


def _component_bounds(component):
    """Overall (min_lon, min_lat, max_lon, max_lat) of the component."""
    all_bounds = [
        b for b in (overpass.bounds_of(way) for way in component) if b
    ]
    if not all_bounds:
        return None
    return (
        min(b[0] for b in all_bounds),
        min(b[1] for b in all_bounds),
        max(b[2] for b in all_bounds),
        max(b[3] for b in all_bounds),
    )


def _create_name_anchor(name, feature_class, click, areas=None):
    # The click is the anchor here — Overpass knows nothing about this
    # place — so it is also the right point to qualify from, and the city
    # rung needs no offset guard: `is_in` was asked about this very point.
    area = nearest_admin_area(click)
    return Place.objects.create(
        slug=unique_slug(
            name,
            admin_qualifier(area, name, None, feature_class),
            locality=locality_qualifier(
                overpass.locality_name(areas or []), name, area
            ),
        ),
        **_admin_context(area),
        anchor_level=Place.AnchorLevel.NAME,
        display_name=name,
        feature_class=feature_class,
        geometry=click,
        centroid=click,
        label_point=click,
    )


def _admin_context(area):
    """Place fields recording where an area lookup landed.

    Empty dict when nothing was found, so the columns keep their blank
    defaults rather than being written as the string 'None'.
    """
    if area is None:
        return {}
    return {
        'admin_country': area.country,
        'admin_country_iso': area.country_iso,
        'admin_subdivision': area.subdivision,
        'admin_subdivision_iso': area.subdivision_iso,
    }
