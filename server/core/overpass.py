"""Overpass API client and element selection for click resolution.

Pure logic, no Django imports: everything here works on plain dicts as
returned by the Overpass JSON API.
"""

import math
import re

import requests

# Tried in order: overpass-api.de rate-limits to 2 concurrent slots per
# IP, so fall back to the Kumi Systems mirror when it rejects us.
OVERPASS_URLS = [
    'https://overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
]
TIMEOUT_S = 15
# overpass-api.de 406es generic client user agents; identify ourselves.
USER_AGENT = 'toponymia/0.1 (dherron@mailbox.org)'

# Click radius: ~20px at the given zoom/latitude, clamped so low zooms
# don't sweep in half a country and high zooms still catch label offsets.
MIN_RADIUS_M = 50
MAX_RADIUS_M = 10_000

QID_RE = re.compile(r'^Q\d+$')

_TYPE_RANK = {'relation': 0, 'way': 1, 'node': 2}


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
    """All named OSM elements matching `name` around the click point.

    Uses `out bb` so ways/relations come back with a bounding box but not
    their (potentially huge) full geometry. (`center` can't be combined
    with `bb`; center_of derives a point from the bounds instead.)
    """
    query = (
        '[out:json][timeout:10];'
        f'nwr["name"="{_escape(name)}"](around:{radius},{lat},{lon});'
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


def _call(query):
    error = None
    for url in OVERPASS_URLS:
        try:
            response = requests.post(
                url,
                data={'data': query},
                headers={'User-Agent': USER_AGENT},
                timeout=TIMEOUT_S,
            )
            response.raise_for_status()
            return response.json().get('elements', [])
        except (requests.RequestException, ValueError) as exc:
            error = exc
    raise OverpassError(str(error)) from error


def choose_element(elements):
    """Pick the best anchor candidate per the ladder in DESIGN.md §3.

    Elements carrying a wikidata QID win (they anchor at level 1), then
    relations over ways over nodes ("prefer the relation that ways belong
    to"), then proximity to the click as a tiebreak.
    """
    if not elements:
        return None

    def sort_key(element):
        has_qid = qid_of(element) is not None
        return (0 if has_qid else 1, _TYPE_RANK.get(element['type'], 3))

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


def bounds_of(element):
    """(min_lon, min_lat, max_lon, max_lat) or None."""
    bounds = element.get('bounds')
    if bounds:
        return (
            bounds['minlon'], bounds['minlat'],
            bounds['maxlon'], bounds['maxlat'],
        )
    return None
