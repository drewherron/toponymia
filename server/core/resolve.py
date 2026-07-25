"""Server-side click resolution: name+point -> anchored Place.

Ladder (DESIGN.md §3.1): cached Place -> Overpass around-query ->
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
from django.utils.text import slugify

from . import overpass
from .models import Place


def resolve(name, feature_class, lng, lat, zoom=None, name_en=None,
            qid=None):
    """Return (place, created). Raises overpass.OverpassError on outage.

    `name` is the feature's native OSM name (what Overpass matches on);
    `name_en` is the English-first label the client displayed, preferred
    for display_name so the article is titled what the user clicked.

    `qid` is an optional Wikidata hint from a caller that already knows
    the entity. It short-circuits the name guess: an existing Place with
    that QID wins outright, else Overpass is queried by wikidata tag,
    which anchors at level 1 by construction. A miss falls through to the
    name ladder below, so a wrong or stale hint costs a query, not a
    resolution.
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

    element = None
    if qid:
        element = overpass.choose_element(
            overpass.fetch_by_qid(qid, lat, lng)
        )
    if element is None:
        element = overpass.choose_element(
            overpass.fetch_elements(name, lat, lng, radius)
        )
    if element is None:
        return _create_name_anchor(name_en or name, feature_class, click), True

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
            component=component, anchor_id=anchor_id,
        ),
        True,
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
                         name_en=None, component=None, anchor_id=None):
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

    return Place.objects.create(
        slug=_unique_slug(display_name),
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
        midpoint = line.interpolate_normalized(0.5)
        return Point(midpoint.x, midpoint.y, srid=4326)
    # Areas and anything else: the guaranteed-interior point.
    return geometry.point_on_surface


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


def _create_name_anchor(name, feature_class, click):
    return Place.objects.create(
        slug=_unique_slug(name),
        anchor_level=Place.AnchorLevel.NAME,
        display_name=name,
        feature_class=feature_class,
        geometry=click,
        centroid=click,
        label_point=click,
    )


def _unique_slug(display_name):
    base = slugify(display_name)[:100] or 'place'
    slug = base
    n = 2
    while Place.objects.filter(slug=slug).exists():
        slug = f'{base}-{n}'
        n += 1
    return slug
