"""Server-side click resolution: name+point -> anchored Place.

Ladder (DESIGN.md §3.1): cached Place -> Overpass around-query ->
wikidata QID (level 1) -> OSM element (level 2) -> name+location (level 3).
"""

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


def resolve(name, feature_class, lng, lat, zoom=None, name_en=None):
    """Return (place, created). Raises overpass.OverpassError on outage.

    `name` is the feature's native OSM name (what Overpass matches on);
    `name_en` is the English-first label the client displayed, preferred
    for display_name so the article is titled what the user clicked.
    """
    radius = overpass.radius_for_click(zoom, lat)
    click = Point(lng, lat, srid=4326)

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
    qid = overpass.qid_of(element) or _component_qid(component)

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
    # relations: centroid+bbox only for now (full geometry deferred to M4)

    center = overpass.center_of(element)
    if component and bounds:
        # center of the whole component, not the clicked segment
        center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
    centroid = Point(*center, srid=4326) if center else click
    if element['type'] == 'node':
        geometry = centroid

    bbox = Polygon.from_bbox(bounds) if bounds else None

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
        # a node IS the feature; for ways/relations the click is the only
        # point known to lie on it (bbox centroids can sit far off)
        label_point=centroid if element['type'] == 'node' else click,
        bbox=bbox,
    )


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
