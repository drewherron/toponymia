"""Server-side click resolution: name+point -> anchored Place.

Ladder (DESIGN.md §3.1): cached Place -> Overpass around-query ->
wikidata QID (level 1) -> OSM element (level 2) -> name+location (level 3).
"""

from django.contrib.gis.geos import LineString, Point, Polygon
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

    qid = overpass.qid_of(element)
    if qid:
        existing = Place.objects.filter(wikidata_qid=qid).first()
        if existing:
            return existing, False
    else:
        existing = Place.objects.filter(
            osm_type=element['type'], osm_id=element['id']
        ).first()
        if existing:
            return existing, False

    return (
        _create_from_element(element, qid, name, feature_class, click, name_en),
        True,
    )


def _create_from_element(element, qid, name, feature_class, click, name_en=None):
    tags = element.get('tags', {})
    display_name = (
        tags.get('name:en') or name_en or tags.get('name') or name
    )
    center = overpass.center_of(element)
    centroid = Point(*center, srid=4326) if center else click

    geometry = None
    if element['type'] == 'node':
        geometry = centroid
    elif element['type'] == 'way':
        coords = overpass.fetch_way_geometry(element['id'])
        if coords and len(coords) >= 2:
            geometry = LineString(coords, srid=4326)
    # relations: centroid+bbox only for now (full geometry deferred to M4)

    bounds = overpass.bounds_of(element)
    bbox = Polygon.from_bbox(bounds) if bounds else None

    return Place.objects.create(
        slug=_unique_slug(display_name),
        wikidata_qid=qid,
        osm_type=element['type'],
        osm_id=element['id'],
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
