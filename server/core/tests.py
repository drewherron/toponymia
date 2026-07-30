import re
import shutil
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import requests
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.contrib.gis.geos import (
    LineString,
    MultiLineString,
    Point,
    Polygon,
)
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import dashboard, overpass, views
from .articles import save_edit
from .terms import TERMS_VERSION, documented_version
from .models import (
    Article,
    Ban,
    BannedEmail,
    ModAction,
    Place,
    PlaceName,
    PlaceSlug,
    Report,
    Revision,
    TalkPost,
    TalkThread,
    TermsAcceptance,
)
from .overpass import (
    OverpassError,
    bounds_of,
    center_of,
    choose_element,
    fetch_elements,
    fetch_relation_member_ways,
    qid_of,
    radius_for_click,
)
from .resolve import representative_point, simplified
from .serializers import (
    MAX_BAN_DAYS,
    MAX_MARKDOWN,
    MAX_NAMES,
    MAX_REFERENCES,
)


class ApiTestCase(TestCase):
    """Base for API tests: clears the throttle cache before each test so
    DRF's per-endpoint rate limits (shared LocMemCache) don't bleed across
    the suite while still being exercised within a test."""

    def setUp(self):
        super().setUp()
        cache.clear()


def _relation(osm_id=1236, name='Mississippi River', qid='Q1497', **extra):
    tags = {'name': name}
    if qid:
        tags['wikidata'] = qid
    return {
        'type': 'relation',
        'id': osm_id,
        'tags': tags,
        'center': {'lat': 32.0, 'lon': -91.0},
        'bounds': {
            'minlat': 29.0, 'minlon': -95.2, 'maxlat': 47.4, 'maxlon': -89.1,
        },
        **extra,
    }


def _way(osm_id=42, name='Mill Creek', qid=None):
    tags = {'name': name}
    if qid:
        tags['wikidata'] = qid
    return {
        'type': 'way',
        'id': osm_id,
        'tags': tags,
        'center': {'lat': 45.1, 'lon': -122.5},
        'bounds': {
            'minlat': 45.0, 'minlon': -122.6, 'maxlat': 45.2, 'maxlon': -122.4,
        },
    }


def _component_way(osm_id, coords, name='Mill Creek', qid=None):
    """A member way as returned by fetch_way_component (`out geom`):
    tags + bounds + coordinate list."""
    tags = {'name': name}
    if qid:
        tags['wikidata'] = qid
    lons = [lon for lon, _ in coords]
    lats = [lat for _, lat in coords]
    return {
        'type': 'way',
        'id': osm_id,
        'tags': tags,
        'bounds': {
            'minlat': min(lats), 'minlon': min(lons),
            'maxlat': max(lats), 'maxlon': max(lons),
        },
        'geometry': [{'lat': lat, 'lon': lon} for lon, lat in coords],
    }


def _sitemap_xml(response):
    """The sitemap streams, so it has `streaming_content` and no `.content`."""
    return b''.join(response.streaming_content).decode()


class HealthTests(TestCase):
    def test_health_returns_ok(self):
        response = self.client.get(reverse('core:health'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})


class OverpassLogicTests(TestCase):
    def test_qid_element_beats_relation_without(self):
        no_qid_relation = _relation(osm_id=7, qid=None)
        node = {
            'type': 'node', 'id': 3, 'lat': 32.0, 'lon': -91.0,
            'tags': {'name': 'X', 'wikidata': 'Q5'},
        }
        self.assertEqual(choose_element([no_qid_relation, node]), node)

    def test_relation_beats_way_and_node(self):
        way = _way(qid='Q1')
        relation = _relation(qid='Q1')
        self.assertEqual(choose_element([way, relation]), relation)

    def test_malformed_qid_ignored(self):
        self.assertIsNone(qid_of(_relation(qid='not-a-qid')))

    def test_center_falls_back_to_bounds_midpoint(self):
        relation = _relation()
        del relation['center']
        lon, lat = center_of(relation)
        self.assertAlmostEqual(lon, -92.15)
        self.assertAlmostEqual(lat, 38.2)

    def test_radius_clamped(self):
        self.assertEqual(radius_for_click(1, 0), 10_000)
        self.assertEqual(radius_for_click(19, 60), 50)
        self.assertEqual(radius_for_click(None, 45), 500)

    def test_boundary_relation_beats_land_area(self):
        """Both carry wikidata=Q142; the land mass has the lower id.

        Without the type tiebreak, France resolved to a place titled
        "France (land mass)".
        """
        land = _relation(osm_id=11980, name='France (terres)', qid='Q142')
        land['tags']['type'] = 'land_area'
        boundary = _relation(osm_id=2202162, name='France', qid='Q142')
        boundary['tags']['type'] = 'boundary'
        self.assertEqual(choose_element([land, boundary]), boundary)
        self.assertEqual(choose_element([boundary, land]), boundary)

    def test_bounds_kept_for_an_ordinary_extent(self):
        self.assertEqual(
            bounds_of(_relation()), (-95.2, 29.0, -89.1, 47.4)
        )

    def test_wrapped_bounds_rejected(self):
        """France: minlon > maxlon means "east, round the globe, to maxlon".

        Stored verbatim it normalises to the extent's complement — a
        0.002°-wide ribbon down the prime meridian — and frames the whole
        world on zoom-to-place.
        """
        france = _relation(
            osm_id=2202162, name='France', qid='Q142',
            bounds={
                'minlat': -50.2187169, 'minlon': 0.0002451,
                'maxlat': 51.3055721, 'maxlon': -0.0012556,
            },
        )
        self.assertIsNone(bounds_of(france))

    def test_bounds_wider_than_half_the_globe_rejected(self):
        """The United States: Guam (144.4E) to Maine (64.4W).

        151° across the Pacific, which as a planar rectangle reads as 209°
        across the Atlantic — excluding Hawaii and most of Alaska.
        """
        usa = _relation(
            osm_id=148838, name='United States', qid='Q30',
            bounds={
                'minlat': -14.7608358, 'minlon': 144.4129186,
                'maxlat': 71.5889534, 'maxlon': -64.35549,
            },
        )
        self.assertIsNone(bounds_of(usa))
        # ...and the same numbers un-wrapped are still too wide to trust.
        self.assertIsNone(
            bounds_of(_relation(bounds={
                'minlat': -14.76, 'minlon': -64.35549,
                'maxlat': 71.59, 'maxlon': 144.4129186,
            }))
        )

    def test_zero_width_bounds_kept(self):
        """A due-north-south way is legitimately zero-wide — not wrapped."""
        vertical = _relation(bounds={
            'minlat': 45.4, 'minlon': -122.7, 'maxlat': 45.5,
            'maxlon': -122.7,
        })
        self.assertEqual(bounds_of(vertical), (-122.7, 45.4, -122.7, 45.5))

    def test_wrapped_bounds_give_no_centre_either(self):
        """center_of averages the bounds, so a wrapped box lands nowhere
        near the feature — France's midpoint would sit in the Gulf of
        Guinea. Falling through to None lets resolve() use the click."""
        france = _relation(bounds={
            'minlat': -50.2, 'minlon': 0.0002, 'maxlat': 51.3,
            'maxlon': -0.0013,
        })
        del france['center']
        self.assertIsNone(center_of(france))


class _FakeResponse:
    def __init__(self, elements=None, http_error=None):
        self._elements = elements or []
        self._http_error = http_error

    def raise_for_status(self):
        if self._http_error:
            raise self._http_error

    def json(self):
        return {'elements': self._elements}


class OverpassCallTests(TestCase):
    """The HTTP layer: mirror fallback + transient-failure retry."""

    def _transient(self):
        return requests.exceptions.ConnectionError('no free slot')

    @patch('core.overpass.time.sleep')
    @patch('core.overpass.requests.post')
    def test_retries_transient_failure_then_succeeds(self, post, sleep):
        # Every mirror fails on the first pass, then one succeeds on retry.
        from core.overpass import OVERPASS_URLS
        good = _FakeResponse(elements=[_relation()])
        first_pass = [self._transient()] * len(OVERPASS_URLS)
        post.side_effect = [*first_pass, good]
        elements = fetch_elements('Mississippi River', 32.0, -91.0, 500)
        self.assertEqual(len(elements), 1)
        self.assertEqual(post.call_count, len(OVERPASS_URLS) + 1)
        sleep.assert_called()  # backed off before the retry pass

    def test_known_bad_mirrors_stay_out_of_the_list(self):
        """Guard the vetting rule documented on OVERPASS_URLS.

        Every host here was in the list once and cost us either time or
        correctness. The first three never answer at all, so they only
        ever added connect-timeout latency to a failing resolution. osm.ch
        is worse: a Switzerland-only extract that returns 200 with zero
        elements for the rest of the planet, which resolve() cannot tell
        from "no such feature here" — it would silently anchor bogus
        level-3 places. Re-adding any of them from the OSM wiki's instance
        list would be a regression, not a redundancy win.
        """
        from core.overpass import OVERPASS_URLS

        for host in (
            'overpass.kumi.systems',
            'overpass.private.coffee',
            'overpass.osm.jp',
            'overpass.osm.ch',
        ):
            for url in OVERPASS_URLS:
                self.assertNotIn(host, url)

    @patch('core.overpass.time.sleep')
    @patch('core.overpass.requests.post')
    def test_raises_after_exhausting_retries(self, post, sleep):
        post.side_effect = self._transient()
        with self.assertRaises(OverpassError):
            fetch_elements('Nowhere', 0.0, 0.0, 500)
        # first pass + every backoff pass, across every mirror
        from core.overpass import OVERPASS_URLS, RETRY_BACKOFFS_S
        passes = 1 + len(RETRY_BACKOFFS_S)
        self.assertEqual(post.call_count, passes * len(OVERPASS_URLS))


class ResolveApiTests(ApiTestCase):
    """Resolution as a signed-in user. Creating a place calls Overpass and
    writes a permanent row, so it needs an account; the anonymous half of
    the endpoint has its own class below."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('resolver', password='pw12345!')
        self.client.force_login(self.user)

    def _post(self, **overrides):
        payload = {
            'name': 'Mississippi River',
            'class': 'waterway',
            'lngLat': [-91.0, 32.0],
            'zoom': 8,
        }
        payload.update(overrides)
        return self.client.post(
            reverse('core:resolve'), payload, content_type='application/json'
        )

    def test_rejects_bad_payload(self):
        response = self._post(lngLat=[-91.0])
        self.assertEqual(response.status_code, 400)
        response = self._post(name='')
        self.assertEqual(response.status_code, 400)
        response = self._post(lngLat=[-191.0, 32.0])
        self.assertEqual(response.status_code, 400)

    @patch('core.resolve.overpass.fetch_elements')
    def test_wikidata_anchor_created(self, fetch):
        fetch.return_value = [_relation()]
        response = self._post()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['created'])
        place = body['place']
        self.assertEqual(place['anchor_level'], 'wikidata')
        self.assertEqual(place['wikidata_qid'], 'Q1497')
        self.assertEqual(place['osm_type'], 'relation')
        self.assertEqual(place['slug'], 'mississippi-river')
        db_place = Place.objects.get(pk=place['id'])
        self.assertIsNotNone(db_place.bbox)
        # dots hang at the click, the only point known to be ON the river
        self.assertEqual(
            (db_place.label_point.x, db_place.label_point.y), (-91.0, 32.0)
        )

    @patch('core.resolve.overpass.fetch_elements')
    def test_antimeridian_country_stores_no_bbox(self, fetch):
        """France resolves to a usable place with *no* footprint.

        A wrapped extent has no planar rectangle, so the place keeps only
        points — and the client picks its zoom from feature_class rather
        than framing a bogus box (which was the whole world).
        """
        france = _relation(
            osm_id=2202162, name='France', qid='Q142',
            bounds={
                'minlat': -50.2187169, 'minlon': 0.0002451,
                'maxlat': 51.3055721, 'maxlon': -0.0012556,
            },
        )
        # We query with `out bb`, which carries no center (M2 finding), so
        # the centroid is derived from the bounds — and must not be.
        del france['center']
        fetch.return_value = [france]
        body = self._post(
            name='France', **{'class': 'country'}, lngLat=[2.3, 46.6]
        ).json()
        place = body['place']
        self.assertEqual(place['anchor_level'], 'wikidata')
        self.assertIsNone(place['bbox'])
        db_place = Place.objects.get(pk=place['id'])
        self.assertIsNone(db_place.bbox)
        # The click is on the mainland; that's what fly-to gets to use.
        self.assertEqual(
            (db_place.label_point.x, db_place.label_point.y), (2.3, 46.6)
        )
        # Not the bounds' midpoint, which for France is the Gulf of Guinea.
        self.assertEqual(
            (db_place.centroid.x, db_place.centroid.y), (2.3, 46.6)
        )

    @patch('core.resolve.overpass.fetch_elements')
    def test_same_qid_from_distant_click_reuses_place(self, fetch):
        fetch.return_value = [_relation()]
        first = self._post().json()
        # well outside the proximity cache (incl. bbox): same QID reused.
        second = self._post(lngLat=[-88.0, 44.5]).json()
        fetch.assert_called()
        self.assertFalse(second['created'])
        self.assertEqual(second['place']['id'], first['place']['id'])
        self.assertEqual(Place.objects.count(), 1)

    @patch('core.resolve.overpass.fetch_elements')
    def test_low_zoom_click_inside_bbox_hits_cache(self, fetch):
        fetch.return_value = [_relation()]
        first = self._post().json()
        fetch.reset_mock()
        # zoomed far out, ~1300 km from centroid and label point but
        # inside the cached bbox: must not re-create the place
        second = self._post(lngLat=[-90.0, 44.0], zoom=5).json()
        fetch.assert_not_called()
        self.assertFalse(second['created'])
        self.assertEqual(second['place']['id'], first['place']['id'])

    @patch('core.resolve.overpass.fetch_elements')
    def test_nearby_click_hits_cache_without_overpass(self, fetch):
        fetch.return_value = [_relation()]
        first = self._post().json()
        fetch.reset_mock()
        second = self._post(lngLat=[-91.0005, 32.0005]).json()
        fetch.assert_not_called()
        self.assertFalse(second['created'])
        self.assertEqual(second['place']['id'], first['place']['id'])

    @patch('core.resolve.overpass.fetch_way_component')
    @patch('core.resolve.overpass.fetch_way_geometry')
    @patch('core.resolve.overpass.fetch_elements')
    def test_osm_anchor_when_no_qid(self, fetch, fetch_geom, fetch_comp):
        # component walk down: falls back to the single-way behavior
        fetch.return_value = [_way()]
        fetch_geom.return_value = [(-122.6, 45.0), (-122.4, 45.2)]
        fetch_comp.side_effect = OverpassError('slot busy')
        place = self._post(
            name='Mill Creek', lngLat=[-122.5, 45.1]
        ).json()['place']
        self.assertEqual(place['anchor_level'], 'osm')
        self.assertIsNone(place['wikidata_qid'])
        self.assertEqual(place['osm_type'], 'way')
        self.assertEqual(place['osm_id'], 42)
        db_place = Place.objects.get(pk=place['id'])
        self.assertEqual(db_place.geometry.geom_type, 'LineString')

    @patch('core.resolve.overpass.fetch_way_component')
    @patch('core.resolve.overpass.fetch_elements')
    def test_way_component_cached_whole_with_min_id_anchor(
        self, fetch, fetch_comp
    ):
        # OSM splits a road into many ways; the whole same-name component
        # is cached as the geometry and the min member id is the anchor.
        fetch.return_value = [_way()]
        fetch_comp.return_value = [
            _component_way(42, [(-122.6, 45.0), (-122.4, 45.2)]),
            _component_way(7, [(-122.4, 45.2), (-122.0, 45.4)]),
        ]
        place = self._post(
            name='Mill Creek', lngLat=[-122.5, 45.1]
        ).json()['place']
        fetch_comp.assert_called_once_with(42, 'Mill Creek')
        self.assertEqual(place['osm_type'], 'way')
        self.assertEqual(place['osm_id'], 7)
        db_place = Place.objects.get(pk=place['id'])
        self.assertEqual(db_place.geometry.geom_type, 'MultiLineString')
        self.assertEqual(len(db_place.geometry), 2)
        # bbox spans the whole component, not just the clicked segment
        self.assertEqual(db_place.bbox.extent, (-122.6, 45.0, -122.0, 45.4))

    @patch('core.resolve.overpass.fetch_way_component')
    @patch('core.resolve.overpass.fetch_elements')
    def test_distant_segment_click_reuses_place(self, fetch, fetch_comp):
        fetch.return_value = [_way()]
        fetch_comp.return_value = [
            _component_way(42, [(-122.6, 45.0), (-122.4, 45.2)]),
            _component_way(7, [(-122.4, 45.2), (-122.0, 45.4)]),
        ]
        first = self._post(name='Mill Creek', lngLat=[-122.5, 45.1]).json()
        # a click on another segment, outside every cached footprint:
        # the walk from that segment shares the min id -> same place
        fetch.return_value = [_way(osm_id=43)]
        fetch_comp.return_value = [
            _component_way(43, [(-121.0, 45.6), (-120.8, 45.7)]),
            _component_way(7, [(-122.4, 45.2), (-122.0, 45.4)]),
        ]
        second = self._post(name='Mill Creek', lngLat=[-120.9, 45.65]).json()
        self.assertFalse(second['created'])
        self.assertEqual(second['place']['id'], first['place']['id'])
        self.assertEqual(Place.objects.count(), 1)

    @patch('core.resolve.overpass.fetch_way_component')
    @patch('core.resolve.overpass.fetch_elements')
    def test_component_sibling_qid_upgrades_anchor(self, fetch, fetch_comp):
        # the clicked segment has no wikidata tag but a sibling does
        fetch.return_value = [_way()]
        fetch_comp.return_value = [
            _component_way(42, [(-122.6, 45.0), (-122.4, 45.2)]),
            _component_way(7, [(-122.4, 45.2), (-122.0, 45.4)], qid='Q555'),
        ]
        place = self._post(
            name='Mill Creek', lngLat=[-122.5, 45.1]
        ).json()['place']
        self.assertEqual(place['anchor_level'], 'wikidata')
        self.assertEqual(place['wikidata_qid'], 'Q555')
        self.assertEqual(place['osm_id'], 7)

    @patch('core.resolve.overpass.fetch_way_component')
    @patch('core.resolve.overpass.fetch_elements')
    def test_component_sibling_qid_matches_existing_place(
        self, fetch, fetch_comp
    ):
        existing = Place.objects.create(
            slug='mill-creek',
            anchor_level=Place.AnchorLevel.WIKIDATA,
            wikidata_qid='Q555',
            osm_type='way',
            osm_id=7,
            display_name='Mill Creek',
            feature_class='waterway',
            centroid=Point(0.0, 0.0, srid=4326),
        )
        fetch.return_value = [_way()]
        fetch_comp.return_value = [
            _component_way(42, [(-122.6, 45.0), (-122.4, 45.2)], qid='Q555'),
        ]
        body = self._post(name='Mill Creek', lngLat=[-122.5, 45.1]).json()
        self.assertFalse(body['created'])
        self.assertEqual(body['place']['id'], existing.id)

    @patch('core.resolve.overpass.fetch_way_component')
    @patch('core.resolve.overpass.fetch_elements')
    def test_single_way_component(self, fetch, fetch_comp):
        fetch.return_value = [_way()]
        fetch_comp.return_value = [
            _component_way(42, [(-122.6, 45.0), (-122.4, 45.2)]),
        ]
        place = self._post(
            name='Mill Creek', lngLat=[-122.5, 45.1]
        ).json()['place']
        self.assertEqual(place['osm_id'], 42)
        db_place = Place.objects.get(pk=place['id'])
        self.assertEqual(db_place.geometry.geom_type, 'MultiLineString')
        self.assertEqual(len(db_place.geometry), 1)

    @patch('core.resolve.overpass.fetch_elements')
    def test_name_anchor_when_overpass_empty(self, fetch):
        fetch.return_value = []
        body = self._post(name='Vanished Hamlet', **{'class': 'city'}).json()
        place = body['place']
        self.assertEqual(place['anchor_level'], 'name')
        self.assertIsNone(place['osm_id'])
        self.assertEqual(place['centroid'], [-91.0, 32.0])
        db_place = Place.objects.get(pk=place['id'])
        self.assertEqual(
            (db_place.label_point.x, db_place.label_point.y), (-91.0, 32.0)
        )

    @patch('core.resolve.overpass.fetch_elements')
    def test_overpass_outage_returns_503_and_creates_nothing(self, fetch):
        fetch.side_effect = OverpassError('boom')
        response = self._post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(Place.objects.count(), 0)

    @patch('core.resolve.overpass.fetch_elements')
    def test_slug_collision_gets_suffix(self, fetch):
        fetch.return_value = []
        self._post(name='Springfield', **{'class': 'city'})
        second = self._post(
            name='Springfield', **{'class': 'city'}, lngLat=[10.0, 50.0]
        ).json()['place']
        self.assertEqual(second['slug'], 'springfield-2')

    @patch('core.resolve.overpass.fetch_elements')
    def test_element_name_en_tag_wins_display_name(self, fetch):
        element = _relation(name='Kalaallit Nunaat', qid='Q223')
        element['tags']['name:en'] = 'Greenland'
        fetch.return_value = [element]
        place = self._post(
            name='Kalaallit Nunaat', name_en='Grønland', **{'class': 'country'}
        ).json()['place']
        self.assertEqual(place['display_name'], 'Greenland')
        self.assertEqual(place['slug'], 'greenland')

    @patch('core.resolve.overpass.fetch_elements')
    def test_client_name_en_used_when_tags_lack_english(self, fetch):
        fetch.return_value = [_relation(name='Kalaallit Nunaat', qid='Q223')]
        place = self._post(
            name='Kalaallit Nunaat', name_en='Greenland', **{'class': 'country'}
        ).json()['place']
        self.assertEqual(place['display_name'], 'Greenland')

    @patch('core.resolve.overpass.fetch_elements')
    def test_cache_matches_english_display_name_from_raw_click(self, fetch):
        element = _relation(name='Kalaallit Nunaat', qid='Q223')
        element['tags']['name:en'] = 'Greenland'
        fetch.return_value = [element]
        first = self._post(
            name='Kalaallit Nunaat', name_en='Greenland', **{'class': 'country'}
        ).json()
        fetch.reset_mock()
        second = self._post(
            name='Kalaallit Nunaat', name_en='Greenland', **{'class': 'country'}
        ).json()
        fetch.assert_not_called()
        self.assertFalse(second['created'])
        self.assertEqual(second['place']['id'], first['place']['id'])

    @patch('core.resolve.overpass.fetch_elements')
    def test_name_anchor_titled_by_displayed_name(self, fetch):
        fetch.return_value = []
        place = self._post(
            name='Íslandsfjall', name_en='Iceland Mountain', **{'class': 'peak'}
        ).json()['place']
        self.assertEqual(place['display_name'], 'Iceland Mountain')
        self.assertEqual(place['anchor_level'], 'name')


class RepresentativePointTests(TestCase):
    """Snapping the label point onto the middle of a feature."""

    def _line(self, coords):
        return LineString(coords, srid=4326)

    def test_midpoint_of_a_straight_line(self):
        point = representative_point(self._line([(0, 0), (10, 0)]))
        self.assertAlmostEqual(point.x, 5.0)
        self.assertAlmostEqual(point.y, 0.0)

    def test_midpoint_follows_the_curve_not_the_bbox(self):
        """The whole point of the change: an L-shaped feature's bbox
        centre is off the feature, its arc midpoint is on it."""
        line = self._line([(0, 0), (0, 10), (10, 10)])
        point = representative_point(line)
        # half of a 20-unit path is the corner
        self.assertAlmostEqual(point.x, 0.0)
        self.assertAlmostEqual(point.y, 10.0)
        # the bbox centre (5, 5) is nowhere near the line
        self.assertGreater(line.distance(Point(5, 5, srid=4326)), 4)

    def test_segments_are_merged_before_measuring(self):
        """Segments in OSM order, not draw order, still yield the middle."""
        pieces = MultiLineString([
            self._line([(6, 0), (10, 0)]),
            self._line([(0, 0), (3, 0)]),
            self._line([(3, 0), (6, 0)]),
        ], srid=4326)
        self.assertAlmostEqual(representative_point(pieces).x, 5.0)

    def test_small_gaps_are_stitched_before_measuring(self):
        """LineMerge needs an exactly shared node, but real relations have
        breaks — the Mississippi's course splits either side of a 950 m
        gap near La Crosse. Left unstitched, "longest chain" means the
        lower 69% of the river and the midpoint lands ~343 km too far
        downstream. Here: two runs of 10 with a gap, so the true midpoint
        is the seam, not the middle of either run.
        """
        pieces = MultiLineString([
            self._line([(0, 0), (10, 0)]),
            self._line([(10.008, 0), (20.008, 0)]),   # ~950 m break
        ], srid=4326)
        point = representative_point(pieces)
        self.assertAlmostEqual(point.x, 10.004, places=2)

    def test_gaps_too_wide_to_be_one_feature_are_not_stitched(self):
        pieces = MultiLineString([
            self._line([(0, 0), (10, 0)]),
            self._line([(40, 0), (44, 0)]),   # a different feature
        ], srid=4326)
        # falls back to the longest run, whose midpoint is 5
        self.assertAlmostEqual(representative_point(pieces).x, 5.0)

    def test_longest_chain_wins_when_pieces_do_not_join(self):
        """A stray disconnected member must not become the course."""
        pieces = MultiLineString([
            self._line([(0, 0), (10, 0)]),      # the main course
            self._line([(50, 50), (50, 51)]),   # an orphan
        ], srid=4326)
        self.assertAlmostEqual(representative_point(pieces).x, 5.0)

    def test_point_geometry_is_returned_unchanged(self):
        point = Point(3, 4, srid=4326)
        self.assertEqual(representative_point(point), point)

    def test_none_geometry_is_none(self):
        self.assertIsNone(representative_point(None))


class SimplifyGeometryTests(TestCase):
    """Stored geometry is thinned to a tolerance scaled to the feature's
    own extent, so the error is sub-pixel at the zoom that frames it."""

    def _zigzag(self, span, wobble, n=400):
        """A line `span` degrees long with `wobble`-degree detours."""
        return LineString(
            [
                (span * i / n, wobble if i % 2 else -wobble)
                for i in range(n + 1)
            ],
            srid=4326,
        )

    def test_thinning_scales_with_the_feature(self):
        """The same wobble survives on a small feature and is dropped on a
        large one — a fixed tolerance could not do both."""
        big = simplified(self._zigzag(span=18.0, wobble=0.002))
        small = simplified(self._zigzag(span=0.05, wobble=0.002))
        self.assertLess(len(big.coords), 50)
        self.assertGreater(len(small.coords), 300)

    def test_points_are_untouched(self):
        point = Point(1, 2, srid=4326)
        self.assertEqual(simplified(point), point)

    def test_none_is_untouched(self):
        self.assertIsNone(simplified(None))

    def test_zero_extent_is_untouched(self):
        degenerate = LineString([(5, 5), (5, 5)], srid=4326)
        self.assertEqual(len(simplified(degenerate).coords), 2)

    def test_srid_survives(self):
        self.assertEqual(simplified(self._zigzag(18.0, 0.002)).srid, 4326)

    def test_multilinestring_stays_a_multilinestring(self):
        """GEOS collapses a one-part MultiLineString to a LineString;
        stored line features are entitled to a stable type."""
        one_part = MultiLineString([self._zigzag(18.0, 0.002)], srid=4326)
        thinned = simplified(one_part)
        self.assertEqual(thinned.geom_type, 'MultiLineString')
        self.assertLess(len(thinned[0].coords), 50)
        self.assertEqual(thinned.srid, 4326)


class RelationMemberFetchTests(TestCase):
    """fetch_relation_member_ways drops the members that aren't the
    feature. The Mississippi's relation carries 155 side_stream and 2
    tributary ways beside its 907 main_stream ones; letting those through
    would drag the merged course, and the midpoint taken from it, off the
    river."""

    def _member(self, ref, role, coords):
        return {
            'type': 'way', 'ref': ref, 'role': role,
            'geometry': [{'lat': lat, 'lon': lon} for lon, lat in coords],
        }

    @patch('core.overpass._call')
    def test_excluded_roles_are_dropped(self, call):
        call.return_value = [{
            'type': 'relation', 'id': 1756854,
            'members': [
                self._member(1, 'main_stream', [(-95.0, 47.0), (-95.0, 29.0)]),
                self._member(2, 'side_stream', [(-95.1, 40.0), (-95.1, 39.0)]),
                self._member(3, 'tributary', [(-95.0, 40.0), (-70.0, 40.0)]),
                self._member(4, '', [(-95.0, 29.0), (-95.0, 28.0)]),
            ],
        }]
        ways = fetch_relation_member_ways(1756854)
        self.assertEqual([w['id'] for w in ways], [1, 4])
        # bounds are derived per way, so the geometry builder can use them
        self.assertEqual(ways[0]['bounds']['minlat'], 29.0)

    @patch('core.overpass._call')
    def test_nodes_and_geometryless_members_are_skipped(self, call):
        call.return_value = [{
            'type': 'relation', 'id': 5,
            'members': [
                {'type': 'node', 'ref': 9, 'role': 'label', 'lat': 1, 'lon': 2},
                {'type': 'way', 'ref': 10, 'role': ''},   # no geometry
                self._member(11, '', [(0.0, 0.0), (1.0, 1.0)]),
            ],
        }]
        self.assertEqual([w['id'] for w in fetch_relation_member_ways(5)], [11])


class RelationGeometryResolveTests(ApiTestCase):
    """Line-like relations cache their course and snap the label point;
    area relations keep the click (a ring's midpoint is the city limit)."""

    def setUp(self):
        # These exercise resolution mechanics, which means creating places —
        # an authenticated action. Permissions are covered separately.
        super().setUp()
        self.client.force_login(
            User.objects.create_user('resolver', password='pw12345!')
        )

    def _ways(self, *coord_lists):
        """Member ways as fetch_relation_member_ways returns them — that
        call has already dropped the excluded roles."""
        return [
            _component_way(100 + i, coords, name='Mississippi River')
            for i, coords in enumerate(coord_lists)
        ]

    def _post(self, **overrides):
        payload = {
            'name': 'Mississippi River',
            'class': 'waterway',
            'lngLat': [-95.2075, 47.2397],   # P625: the source, not the middle
            'zoom': 8,
        }
        payload.update(overrides)
        return self.client.post(
            reverse('core:resolve'), payload, content_type='application/json'
        )

    @patch('core.resolve.overpass.fetch_relation_member_ways')
    @patch('core.resolve.overpass.fetch_elements')
    def test_label_point_snaps_to_mid_course_not_the_click(
        self, fetch, members
    ):
        relation = _relation(tags={'name': 'Mississippi River',
                                   'wikidata': 'Q1497', 'type': 'waterway'})
        fetch.return_value = [relation]
        members.return_value = self._ways([(-95.0, 47.0), (-95.0, 29.0)])
        place = Place.objects.get(pk=self._post().json()['place']['id'])
        # the click was the source at lat 47; the dot is now mid-course
        self.assertAlmostEqual(place.label_point.y, 38.0, places=4)
        self.assertIsNotNone(place.geometry)

    @patch('core.resolve.overpass.fetch_relation_member_ways')
    @patch('core.resolve.overpass.fetch_elements')
    def test_label_point_comes_from_the_full_course_not_the_thinned_one(
        self, fetch, members
    ):
        """Order matters. The dot is drawn over the basemap, which renders
        OSM's true geometry, so it must be snapped to the full course —
        thinning first would move it (63 km on the Mississippi, whose
        thinned copy is 4.8% shorter once the meanders go)."""
        fetch.return_value = [_relation(
            tags={'name': 'Mississippi River', 'wikidata': 'Q1497',
                  'type': 'waterway'})]
        # A meandering course: the wobbles carry length that thinning eats.
        # 18 degrees of span -> a 0.0045 degree tolerance, so these
        # sub-tolerance wobbles are exactly what thinning removes.
        coords = [
            (-95.0 + (0.002 if i % 2 else -0.002), 47.0 - i * 0.045)
            for i in range(401)
        ]
        members.return_value = [_component_way(1, coords, name='Mississippi')]
        place = Place.objects.get(pk=self._post().json()['place']['id'])

        stored = place.geometry
        vertices = (
            len(stored.coords) if stored.geom_type == 'LineString'
            else sum(len(part.coords) for part in stored)
        )
        self.assertLess(vertices, len(coords))         # it was thinned
        full = MultiLineString([LineString(coords, srid=4326)], srid=4326)
        self.assertAlmostEqual(
            place.label_point.y, representative_point(full).y, places=6
        )

    @patch('core.resolve.overpass.fetch_relation_member_ways')
    @patch('core.resolve.overpass.fetch_elements')
    def test_area_relation_keeps_the_click(self, fetch, members):
        fetch.return_value = [_relation(
            osm_id=122604, name='Chicago', qid='Q1297',
            tags={'name': 'Chicago', 'wikidata': 'Q1297', 'type': 'boundary'})]
        place = Place.objects.get(pk=self._post(
            name='Chicago', lngLat=[-87.6278, 41.8819], **{'class': 'city'},
        ).json()['place']['id'])
        members.assert_not_called()
        self.assertAlmostEqual(place.label_point.x, -87.6278, places=4)
        self.assertIsNone(place.geometry)

    @patch('core.resolve.overpass.fetch_relation_member_ways')
    @patch('core.resolve.overpass.fetch_elements')
    def test_geometry_fetch_failure_degrades_to_the_click(
        self, fetch, members
    ):
        fetch.return_value = [_relation(
            tags={'name': 'Mississippi River', 'wikidata': 'Q1497',
                  'type': 'waterway'})]
        members.side_effect = OverpassError('no free slot')
        place = Place.objects.get(pk=self._post().json()['place']['id'])
        self.assertAlmostEqual(place.label_point.y, 47.2397, places=4)
        self.assertIsNone(place.geometry)


class ResolveQidHintTests(ApiTestCase):
    """The optional `qid` hint.

    Regression cover for the seeding bot's level-3 anchors: every one of
    Chicago, Beijing, Shenzhen and Chengdu resolved by name and missed,
    two because OSM's node sits further from Wikidata's P625 point than
    the click radius, two because Wikidata's native label and OSM's name
    tag differ (深圳 vs 深圳市; no P1705 at all, so an English label got
    sent at a Chinese name tag). A QID matches all four exactly.
    """

    def setUp(self):
        # Creating places is an authenticated action; see
        # ResolvePermissionTests for the anonymous half of this endpoint.
        super().setUp()
        self.client.force_login(
            User.objects.create_user('resolver', password='pw12345!')
        )

    def _post(self, **overrides):
        payload = {
            'name': 'Chengdu',          # the English fallback that missed
            'class': 'city',
            'lngLat': [104.06333, 30.66],
            'qid': 'Q30002',
        }
        payload.update(overrides)
        return self.client.post(
            reverse('core:resolve'), payload, content_type='application/json'
        )

    def _chengdu(self):
        return _relation(osm_id=2110264, name='成都市', qid='Q30002')

    def test_rejects_malformed_qid(self):
        self.assertEqual(self._post(qid='not-a-qid').status_code, 400)
        self.assertEqual(self._post(qid=30002).status_code, 400)

    @patch('core.resolve.overpass.fetch_elements')
    @patch('core.resolve.overpass.fetch_by_qid')
    def test_hint_anchors_at_level_1_when_name_would_miss(self, by_qid, fetch):
        fetch.return_value = []           # what the name query really returns
        by_qid.return_value = [self._chengdu()]
        place = self._post().json()['place']
        self.assertEqual(place['anchor_level'], 'wikidata')
        self.assertEqual(place['wikidata_qid'], 'Q30002')
        self.assertEqual(place['osm_type'], 'relation')
        # the name query is never reached once the hint matches
        fetch.assert_not_called()

    @patch('core.resolve.overpass.fetch_elements')
    @patch('core.resolve.overpass.fetch_by_qid')
    def test_hint_reuses_existing_place_without_touching_overpass(
        self, by_qid, fetch
    ):
        by_qid.return_value = [self._chengdu()]
        first = self._post().json()
        by_qid.reset_mock()
        # a re-run from anywhere: the QID alone identifies the place, so
        # a seeding bot re-publishing costs no Overpass call at all.
        second = self._post(lngLat=[0.0, 0.0]).json()
        self.assertFalse(second['created'])
        self.assertEqual(second['place']['id'], first['place']['id'])
        by_qid.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(Place.objects.count(), 1)

    @patch('core.resolve.overpass.fetch_elements')
    @patch('core.resolve.overpass.fetch_by_qid')
    def test_stale_hint_falls_through_to_the_name_ladder(self, by_qid, fetch):
        # A QID that OSM doesn't carry costs one query, not a resolution.
        by_qid.return_value = []
        fetch.return_value = [self._chengdu()]
        place = self._post(name='成都市').json()['place']
        fetch.assert_called_once()
        self.assertEqual(place['anchor_level'], 'wikidata')

    @patch('core.resolve.overpass._call')
    def test_qid_query_carries_no_proximity_filter(self, call):
        """A QID is globally unique, so `around` only adds false negatives.

        `["wikidata"="Q30"](around:50000, <centre of the US>)` really does
        return nothing: Overpass counts a relation as near a point only if
        its *members* are, and a country's borders aren't near its middle.
        """
        call.return_value = []
        overpass.fetch_by_qid('Q30')
        query = call.call_args[0][0]
        self.assertIn('["wikidata"="Q30"]', query)
        self.assertNotIn('around', query)

    @patch('core.resolve.overpass.fetch_elements')
    @patch('core.resolve.overpass.fetch_by_qid')
    def test_hint_finds_a_country_from_its_interior(self, by_qid, fetch):
        """The case the radius broke: a click nowhere near the border."""
        usa = _relation(osm_id=148838, name='United States', qid='Q30')
        usa['tags']['type'] = 'boundary'
        by_qid.return_value = [usa]
        fetch.return_value = []
        place = self._post(
            name='United States', **{'class': 'country'},
            lngLat=[-98.5, 39.8], qid='Q30',
        ).json()['place']
        self.assertEqual(place['anchor_level'], 'wikidata')
        self.assertEqual(place['osm_id'], 148838)
        fetch.assert_not_called()

    @patch('core.resolve.overpass.fetch_elements')
    @patch('core.resolve.overpass.fetch_by_qid')
    def test_no_hint_leaves_behavior_unchanged(self, by_qid, fetch):
        fetch.return_value = [self._chengdu()]
        place = self._post(name='成都市', qid=None).json()['place']
        by_qid.assert_not_called()
        self.assertEqual(place['wikidata_qid'], 'Q30002')


def _make_place(name='Testville', slug='testville'):
    return Place.objects.create(
        slug=slug,
        anchor_level=Place.AnchorLevel.NAME,
        display_name=name,
        feature_class='city',
        centroid=Point(10.0, 50.0, srid=4326),
    )


def _content(**overrides):
    content = {
        'body_md': 'Founded as a test fixture in 2026.',
        'names': [
            {
                'name': 'Testville',
                'language': 'eng',
                'is_endonym': True,
                'etymology_md': '*test* + *-ville*',
            },
        ],
    }
    content.update(overrides)
    return content


class ArticleApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.place = _make_place()
        self.user = User.objects.create_user('drew', password='pw12345!')

    def _put(self, content=None, comment='first draft'):
        return self.client.put(
            reverse('core:article-edit', args=[self.place.slug]),
            {'content': content or _content(), 'comment': comment},
            content_type='application/json',
        )

    def test_detail_without_article(self):
        response = self.client.get(
            reverse('core:place-detail', args=[self.place.slug])
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body['article'])
        self.assertEqual(body['place']['slug'], 'testville')

    def test_detail_unknown_slug_404s(self):
        response = self.client.get(
            reverse('core:place-detail', args=['nowhere'])
        )
        self.assertEqual(response.status_code, 404)

    def test_anonymous_edit_rejected(self):
        response = self._put()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Revision.objects.count(), 0)

    def test_create_article(self):
        self.client.force_login(self.user)
        response = self._put()
        self.assertEqual(response.status_code, 200)
        article = response.json()['article']
        self.assertEqual(article['author'], 'drew')
        self.assertEqual(article['comment'], 'first draft')
        self.assertEqual(
            article['content']['names'][0]['name'], 'Testville'
        )
        # serializer fills structural defaults into the stored snapshot
        self.assertEqual(article['content']['derivations'], [])
        names = PlaceName.objects.filter(place=self.place)
        self.assertEqual(names.count(), 1)
        self.assertTrue(names.get().is_endonym)

    def test_edit_appends_revision_and_rematerializes(self):
        self.client.force_login(self.user)
        self._put()
        second = _content(
            names=[
                {'name': 'Testville', 'language': 'eng'},
                {'name': 'Probeburg', 'language': 'deu'},
                {'name': 'Probeburg', 'language': 'deu'},  # dupe collapses
            ]
        )
        response = self._put(content=second, comment='add German exonym')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Revision.objects.count(), 2)
        current = self.place.article.current_revision
        self.assertEqual(current.comment, 'add German exonym')
        self.assertEqual(
            sorted(
                PlaceName.objects.filter(place=self.place).values_list(
                    'name', flat=True
                )
            ),
            ['Probeburg', 'Testville'],
        )

    def test_empty_content_rejected(self):
        self.client.force_login(self.user)
        response = self._put(content={'body_md': '   ', 'names': []})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Revision.objects.count(), 0)

    def test_names_only_content_accepted(self):
        # body_md is vestigial (removed from the UI) — optional on write
        self.client.force_login(self.user)
        content = _content()
        del content['body_md']
        response = self._put(content=content)
        self.assertEqual(response.status_code, 200)
        stored = response.json()['article']['content']
        self.assertEqual(stored['body_md'], '')

    def test_body_only_content_rejected(self):
        # everything belongs to a name now: a body alone isn't an article
        self.client.force_login(self.user)
        response = self._put(content={'body_md': 'A place.', 'names': []})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Revision.objects.count(), 0)

    def test_unknown_language_code_rejected(self):
        self.client.force_login(self.user)
        content = _content()
        content['names'][0]['from_languages'] = ['lat', 'Latin']
        response = self._put(content=content)
        self.assertEqual(response.status_code, 400)
        self.assertIn('Latin', str(response.json()))
        self.assertEqual(Revision.objects.count(), 0)

    def test_language_codes_normalized_to_iso639_3(self):
        # ISO 639-1 two-letter input is accepted and stored as 639-3
        self.client.force_login(self.user)
        content = _content()
        content['names'][0]['language'] = 'FR'
        content['names'][0]['from_languages'] = ['la', 'ang']
        response = self._put(content=content)
        self.assertEqual(response.status_code, 200)
        stored = response.json()['article']['content']['names'][0]
        self.assertEqual(stored['language'], 'fra')
        self.assertEqual(stored['from_languages'], ['lat', 'ang'])
        row = PlaceName.objects.get(place=self.place)
        self.assertEqual(row.language, 'fra')
        self.assertEqual(row.from_languages, ['lat', 'ang'])

    def test_blank_language_still_allowed(self):
        self.client.force_login(self.user)
        content = _content()
        content['names'][0]['language'] = ''
        response = self._put(content=content)
        self.assertEqual(response.status_code, 200)

    def test_detail_returns_current_article(self):
        self.client.force_login(self.user)
        self._put()
        self.client.logout()
        body = self.client.get(
            reverse('core:place-detail', args=[self.place.slug])
        ).json()
        self.assertEqual(body['article']['author'], 'drew')
        self.assertIn('Founded', body['article']['content']['body_md'])

    def test_oversized_etymology_rejected(self):
        self.client.force_login(self.user)
        content = _content()
        content['names'][0]['etymology_md'] = 'x' * (MAX_MARKDOWN + 1)
        response = self._put(content=content)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Revision.objects.count(), 0)

    def test_etymology_at_the_limit_accepted(self):
        self.client.force_login(self.user)
        content = _content()
        content['names'][0]['etymology_md'] = 'x' * MAX_MARKDOWN
        self.assertEqual(self._put(content=content).status_code, 200)

    def test_too_many_names_rejected(self):
        self.client.force_login(self.user)
        content = _content(
            names=[
                {'name': f'Alias {i}', 'language': 'eng'}
                for i in range(MAX_NAMES + 1)
            ]
        )
        response = self._put(content=content)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Revision.objects.count(), 0)

    def test_too_many_references_rejected(self):
        self.client.force_login(self.user)
        content = _content()
        content['names'][0]['references'] = [
            f'ref {i}' for i in range(MAX_REFERENCES + 1)
        ]
        response = self._put(content=content)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Revision.objects.count(), 0)

    def test_revert_ignores_limits_on_historical_content(self):
        # Revisions written before these caps existed (or by a future looser
        # schema) must stay revertable — revert copies the old snapshot
        # through verbatim rather than revalidating it.
        self.client.force_login(self.user)
        self._put()
        oversized = _content()
        oversized['names'][0]['etymology_md'] = 'x' * (MAX_MARKDOWN + 500)
        old = Revision.objects.create(
            article=Article.objects.get(place=self.place),
            author=self.user,
            comment='legacy',
            content=oversized,
        )
        self._put(comment='second')
        response = self.client.post(
            reverse('core:article-revert', args=[self.place.slug]),
            {'revision_id': old.id},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        restored = response.json()['article']['content']['names'][0]
        self.assertEqual(len(restored['etymology_md']), MAX_MARKDOWN + 500)


class RevisionApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.place = _make_place()
        self.user = User.objects.create_user('drew', password='pw12345!')
        self.client.force_login(self.user)
        self._put(comment='first draft')
        self._put(
            content=_content(body_md='Rewritten in revision two.'),
            comment='rewrite',
        )

    def _put(self, content=None, comment=''):
        return self.client.put(
            reverse('core:article-edit', args=[self.place.slug]),
            {'content': content or _content(), 'comment': comment},
            content_type='application/json',
        )

    def test_list_newest_first_marks_current(self):
        response = self.client.get(
            reverse('core:revision-list', args=[self.place.slug])
        )
        self.assertEqual(response.status_code, 200)
        revisions = response.json()['revisions']
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0]['comment'], 'rewrite')
        self.assertTrue(revisions[0]['is_current'])
        self.assertFalse(revisions[1]['is_current'])
        self.assertNotIn('content', revisions[0])

    def test_list_for_stub_is_empty(self):
        stub = _make_place(name='Stubton', slug='stubton')
        response = self.client.get(
            reverse('core:revision-list', args=[stub.slug])
        )
        self.assertEqual(response.json()['revisions'], [])

    def test_detail_carries_content(self):
        first = Revision.objects.order_by('id').first()
        response = self.client.get(
            reverse(
                'core:revision-detail', args=[self.place.slug, first.id]
            )
        )
        self.assertEqual(response.status_code, 200)
        revision = response.json()['revision']
        self.assertIn('Founded', revision['content']['body_md'])
        self.assertFalse(revision['is_current'])

    def test_detail_wrong_slug_404s(self):
        other = _make_place(name='Elsewhere', slug='elsewhere')
        first = Revision.objects.order_by('id').first()
        response = self.client.get(
            reverse('core:revision-detail', args=[other.slug, first.id])
        )
        self.assertEqual(response.status_code, 404)

    def test_revert_copies_snapshot_and_rematerializes(self):
        first = Revision.objects.order_by('id').first()
        second = _content(
            names=[{'name': 'Renamedville', 'language': 'eng'}]
        )
        self._put(content=second, comment='rename')
        response = self.client.post(
            reverse('core:article-revert', args=[self.place.slug]),
            {'revision_id': first.id},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        article = response.json()['article']
        self.assertEqual(article['comment'], f'Reverted to revision {first.id}')
        self.assertIn('Founded', article['content']['body_md'])
        self.assertEqual(Revision.objects.count(), 4)
        self.assertEqual(
            list(
                PlaceName.objects.filter(place=self.place).values_list(
                    'name', flat=True
                )
            ),
            ['Testville'],
        )

    def test_revert_to_current_rejected(self):
        current = self.place.article.current_revision
        response = self.client.post(
            reverse('core:article-revert', args=[self.place.slug]),
            {'revision_id': current.id},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Revision.objects.count(), 2)

    def test_revert_needs_login(self):
        self.client.logout()
        first = Revision.objects.order_by('id').first()
        response = self.client.post(
            reverse('core:article-revert', args=[self.place.slug]),
            {'revision_id': first.id},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_revert_foreign_revision_404s(self):
        other = _make_place(name='Elsewhere', slug='elsewhere')
        first = Revision.objects.order_by('id').first()
        response = self.client.post(
            reverse('core:article-revert', args=[other.slug]),
            {'revision_id': first.id},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)


class TalkApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.place = _make_place()
        self.user = User.objects.create_user('drew', password='pw12345!')
        self.other = User.objects.create_user('sam', password='pw12345!')

    def _create_thread(self, title='Etymology dispute', body='Sources?'):
        return self.client.post(
            reverse('core:talk', args=[self.place.slug]),
            {'title': title, 'body_md': body},
            content_type='application/json',
        )

    def test_get_empty(self):
        response = self.client.get(
            reverse('core:talk', args=[self.place.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['threads'], [])

    def test_anonymous_cannot_post(self):
        self.assertEqual(self._create_thread().status_code, 403)

    def test_thread_reply_and_ordering(self):
        self.client.force_login(self.user)
        thread_id = self._create_thread().json()['thread']['id']
        self._create_thread(title='Second topic')
        self.client.force_login(self.other)
        response = self.client.post(
            reverse('core:talk-reply', args=[thread_id]),
            {'body_md': 'Herodotus, book 4.'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        threads = self.client.get(
            reverse('core:talk', args=[self.place.slug])
        ).json()['threads']
        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0]['title'], 'Etymology dispute')
        posts = threads[0]['posts']
        self.assertEqual(
            [post['author'] for post in posts], ['drew', 'sam']
        )
        self.assertIsNone(posts[0]['edited'])

    def test_blank_post_rejected(self):
        self.client.force_login(self.user)
        response = self._create_thread(body='   ')
        self.assertEqual(response.status_code, 400)

    def test_edit_own_post(self):
        self.client.force_login(self.user)
        post = self._create_thread().json()['thread']['posts'][0]
        response = self.client.put(
            reverse('core:talk-post-edit', args=[post['id']]),
            {'body_md': 'Sources, please?'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        updated = response.json()['post']
        self.assertEqual(updated['body_md'], 'Sources, please?')
        self.assertIsNotNone(updated['edited'])

    def test_cannot_edit_others_post(self):
        self.client.force_login(self.user)
        post = self._create_thread().json()['thread']['posts'][0]
        self.client.force_login(self.other)
        response = self.client.put(
            reverse('core:talk-post-edit', args=[post['id']]),
            {'body_md': 'hijacked'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)


def _publish(place, user):
    """Give a place a current revision, making it a highlight candidate."""
    article = Article.objects.create(place=place)
    revision = Revision.objects.create(
        article=article, author=user, comment='', content=_content()
    )
    article.current_revision = revision
    article.save(update_fields=['current_revision'])


class HighlightApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('drew', password='pw12345!')

    def _get(self, bbox='9,49,11,51'):
        return self.client.get(
            reverse('core:highlights'), {'bbox': bbox}
        )

    def test_rejects_malformed_bbox(self):
        for bbox in ('', '1,2,3', '1,2,3,x'):
            self.assertEqual(self._get(bbox).status_code, 400)

    def test_rejects_non_finite_bbox(self):
        # float() parses these; NaN then defeats every range check (all
        # comparisons against it are False) and used to reach GEOS as a
        # degenerate ring, 500ing the endpoint.
        for bbox in (
            'nan,nan,nan,nan',
            '9,nan,11,51',
            'inf,49,11,51',
            '9,49,11,-inf',
        ):
            with self.subTest(bbox=bbox):
                self.assertEqual(self._get(bbox).status_code, 400)

    def test_place_without_article_excluded(self):
        _make_place()
        body = self._get().json()
        self.assertEqual(body['type'], 'FeatureCollection')
        self.assertEqual(body['features'], [])

    def test_centroid_feature_with_materialized_names(self):
        place = _make_place()  # centroid (10, 50)
        PlaceName.objects.create(place=place, name='Probeburg', language='deu')
        _publish(place, self.user)
        features = self._get().json()['features']
        self.assertEqual(len(features), 1)
        feature = features[0]
        self.assertEqual(feature['geometry']['type'], 'Point')
        self.assertEqual(feature['geometry']['coordinates'], [10.0, 50.0])
        self.assertEqual(feature['properties']['slug'], 'testville')
        self.assertEqual(feature['properties']['display_name'], 'Testville')
        self.assertEqual(
            feature['properties']['names'], ['Probeburg', 'Testville']
        )

    def test_line_place_included_by_geometry_not_centroid(self):
        # River crosses the viewport but its centroid sits far outside:
        # labels in view must still recolor, and the feature stays a
        # centroid point (the client never paints geometry).
        river = Place.objects.create(
            slug='test-river',
            anchor_level=Place.AnchorLevel.OSM,
            osm_type='way',
            osm_id=99,
            display_name='Test River',
            feature_class='waterway',
            geometry=LineString([(9.5, 50.2), (30.0, 55.0)], srid=4326),
            centroid=Point(30.0, 55.0, srid=4326),
        )
        _publish(river, self.user)
        features = self._get().json()['features']
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]['geometry']['type'], 'Point')
        self.assertEqual(features[0]['geometry']['coordinates'], [30.0, 55.0])
        # viewport far away: nothing
        self.assertEqual(self._get('-20,-10,-18,-8').json()['features'], [])

    def test_road_component_lights_up_along_its_length(self):
        # A road cached as a same-name component MultiLineString must be
        # served for a viewport at its far end (the amber-label fix).
        road = Place.objects.create(
            slug='test-road',
            anchor_level=Place.AnchorLevel.OSM,
            osm_type='way',
            osm_id=7,
            display_name='Test Road',
            feature_class='road',
            geometry=MultiLineString(
                LineString([(9.5, 50.2), (9.8, 50.3)], srid=4326),
                LineString([(9.8, 50.3), (10.4, 50.5)], srid=4326),
                srid=4326,
            ),
            centroid=Point(9.95, 50.35, srid=4326),
        )
        _publish(road, self.user)
        # viewport over the second segment only
        features = self._get('10.2,50.4,10.6,50.6').json()['features']
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]['properties']['slug'], 'test-road')
        # viewport off the road entirely: nothing
        self.assertEqual(self._get('12,52,13,53').json()['features'], [])

    def test_label_point_preferred_over_centroid(self):
        place = _make_place()
        place.label_point = Point(10.5, 50.5, srid=4326)
        place.save(update_fields=['label_point'])
        _publish(place, self.user)
        feature = self._get().json()['features'][0]
        self.assertEqual(feature['geometry']['coordinates'], [10.5, 50.5])

    def test_relation_included_by_bbox(self):
        # Relations cache no geometry, only centroid+bbox; the bbox keeps
        # a big river's labels lit anywhere along its course.
        river = Place.objects.create(
            slug='big-river',
            anchor_level=Place.AnchorLevel.WIKIDATA,
            wikidata_qid='Q1497',
            osm_type='relation',
            osm_id=17,
            display_name='Big River',
            feature_class='waterway',
            centroid=Point(30.0, 55.0, srid=4326),
            bbox=Polygon.from_bbox((8.0, 48.0, 31.0, 56.0)),
        )
        _publish(river, self.user)
        features = self._get().json()['features']
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]['properties']['slug'], 'big-river')

    def test_wrapped_viewport_falls_back_to_world(self):
        _publish(_make_place(), self.user)
        features = self._get('150,-60,210,60').json()['features']
        self.assertEqual(len(features), 1)


class PlaceGeometryApiTests(ApiTestCase):
    """The lazily-fetched course behind the "zoom to place" highlight."""

    def _get(self, slug):
        return self.client.get(
            reverse('core:place-geometry', args=[slug])
        )

    def test_returns_cached_course_as_geojson(self):
        place = _make_place(name='Long Creek', slug='long-creek')
        place.geometry = MultiLineString(
            LineString([(9.0, 50.0), (10.0, 51.0)], srid=4326),
            LineString([(10.0, 51.0), (12.0, 52.0)], srid=4326),
            srid=4326,
        )
        place.save()
        body = self._get('long-creek').json()
        self.assertEqual(body['geometry']['type'], 'MultiLineString')
        self.assertEqual(
            body['geometry']['coordinates'],
            [[[9.0, 50.0], [10.0, 51.0]], [[10.0, 51.0], [12.0, 52.0]]],
        )

    def test_area_relation_reports_no_geometry(self):
        # Cities and countries cache centroid+bbox only, so they have
        # nothing to draw — null, not a 404: the place exists.
        _make_place()
        response = self._get('testville')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['geometry'])

    def test_unknown_slug_404s(self):
        self.assertEqual(self._get('nowhere').status_code, 404)

    def test_geometry_stays_out_of_the_detail_response(self):
        # The detail endpoint is fetched on every article open; keeping
        # tens of kB of course off it is the whole point of this endpoint.
        place = _make_place()
        place.geometry = LineString([(9.0, 50.0), (10.0, 51.0)], srid=4326)
        place.save()
        body = self.client.get(
            reverse('core:place-detail', args=['testville'])
        ).json()
        self.assertNotIn('geometry', body['place'])


class AuthApiTests(ApiTestCase):
    def test_me_anonymous(self):
        response = self.client.get(reverse('core:me'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['user'])
        self.assertIn('csrftoken', response.cookies)

    def test_me_logged_in(self):
        user = User.objects.create_user('drew', password='pw12345!')
        self.client.force_login(user)
        self.assertEqual(
            self.client.get(reverse('core:me')).json()['user']['username'],
            'drew',
        )

    def test_signup_requires_email(self):
        response = self.client.post(
            '/_allauth/browser/v1/auth/signup',
            {
                'username': 'newuser',
                'password': 'sturdy-passphrase-9',
                'terms': True,
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username='newuser').exists())

    def test_signup_is_unverified_and_pending(self):
        # With mandatory verification the account is created but the session
        # stays anonymous until the emailed code is confirmed, and a
        # verification email goes out.
        response = self.client.post(
            '/_allauth/browser/v1/auth/signup',
            {
                'username': 'newuser',
                'email': 'newuser@example.com',
                'password': 'sturdy-passphrase-9',
                'terms': True,
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)
        self.assertTrue(User.objects.filter(username='newuser').exists())
        self.assertIsNone(
            self.client.get(reverse('core:me')).json()['user']
        )
        self.assertEqual(len(mail.outbox), 1)

    def test_signup_verify_by_code_authenticates(self):
        self.client.post(
            '/_allauth/browser/v1/auth/signup',
            {
                'username': 'newuser',
                'email': 'newuser@example.com',
                'password': 'sturdy-passphrase-9',
                'terms': True,
            },
            content_type='application/json',
        )
        code = re.search(
            r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b', mail.outbox[0].body
        ).group(1)
        response = self.client.post(
            '/_allauth/browser/v1/auth/email/verify',
            {'key': code},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(reverse('core:me')).json()['user']['username'],
            'newuser',
        )

    # The 11 signups below have to land inside the limit's own 60s window, and
    # a real PBKDF2 hash per signup costs seconds under load — enough that this
    # used to pass only on an idle box. A fast hasher makes it about the rate
    # limit rather than about the machine.
    @override_settings(
        PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
    )
    def test_signup_is_rate_limited(self):
        # signup is capped at 10/min/IP; the test client shares one REMOTE_ADDR,
        # so the 11th distinct signup in a minute is throttled (429).
        statuses = []
        for i in range(11):
            response = self.client.post(
                '/_allauth/browser/v1/auth/signup',
                {
                    'username': f'spammer{i}',
                    'email': f'spammer{i}@example.com',
                    'password': 'sturdy-passphrase-9',
                    'terms': True,
                },
                content_type='application/json',
            )
            statuses.append(response.status_code)
        # first ten accepted (pending verification), eleventh throttled
        self.assertNotIn(429, statuses[:10])
        self.assertEqual(statuses[10], 429)

    def test_login_by_email(self):
        # One field takes either identifier; the SPA posts 'email' when the
        # value contains an "@", so both keys have to authenticate.
        user = User.objects.create_user('drew', password='sturdy-passphrase-9')
        EmailAddress.objects.create(
            user=user, email='drew@example.com', verified=True, primary=True
        )
        response = self.client.post(
            '/_allauth/browser/v1/auth/login',
            {'email': 'drew@example.com', 'password': 'sturdy-passphrase-9'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(reverse('core:me')).json()['user']['username'],
            'drew',
        )

    def test_signup_rejects_at_sign_in_username(self):
        # "@" is what tells the SPA to post the email key, so a username may
        # not contain one — otherwise it could never be posted as a username.
        response = self.client.post(
            '/_allauth/browser/v1/auth/signup',
            {
                'username': 'dr@ew',
                'email': 'atsign@example.com',
                'password': 'sturdy-passphrase-9',
                'terms': True,
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username='dr@ew').exists())

    def test_headless_login_logout(self):
        user = User.objects.create_user('drew', password='sturdy-passphrase-9')
        EmailAddress.objects.create(
            user=user, email='drew@example.com', verified=True, primary=True
        )
        response = self.client.post(
            '/_allauth/browser/v1/auth/login',
            {'username': 'drew', 'password': 'sturdy-passphrase-9'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.delete('/_allauth/browser/v1/auth/session')
        # allauth answers 401 ("no longer authenticated") on logout
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(
            self.client.get(reverse('core:me')).json()['user']
        )


class BannedEmailTests(ApiTestCase):
    """A ban records the account's email in the registration blocklist so the
    same address can't open a fresh account — even after the account is gone."""

    SIGNUP = '/_allauth/browser/v1/auth/signup'

    def setUp(self):
        super().setUp()
        self.mod = User.objects.create_user(
            'mira', password='pw12345!', is_staff=True
        )
        self.target = User.objects.create_user(
            'spammer', password='pw12345!', email='spammer@example.com'
        )
        EmailAddress.objects.create(
            user=self.target, email='spammer@example.com',
            verified=True, primary=True,
        )

    def _ban(self, **body):
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-ban-user', args=[self.target.id]),
            body, content_type='application/json',
        )
        self.client.logout()
        return response

    def _signup(self, email, username='fresh'):
        return self.client.post(
            self.SIGNUP,
            {
                'username': username,
                'email': email,
                'password': 'sturdy-passphrase-9',
                'terms': True,
            },
            content_type='application/json',
        )

    def test_ban_records_account_email(self):
        self._ban(reason='spam')
        block = BannedEmail.objects.get(email='spammer@example.com')
        self.assertTrue(block.is_active())
        self.assertEqual(block.banned_user_id, self.target.id)
        self.assertEqual(block.reason, 'spam')

    def test_blocked_email_cannot_register(self):
        # The durable case: the offending account is gone, but the block holds.
        self._ban(reason='spam')
        self.target.delete()
        response = self._signup('spammer@example.com')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username='fresh').exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_matching_is_case_insensitive(self):
        self._ban(reason='spam')
        self.target.delete()
        self.assertEqual(
            self._signup('Spammer@Example.com').status_code, 400
        )

    def test_unblocked_email_still_registers(self):
        self._ban(reason='spam')
        response = self._signup('someone-else@example.com')
        # Signup succeeds but stays pending verification (anonymous, 401).
        self.assertEqual(response.status_code, 401)
        self.assertTrue(User.objects.filter(username='fresh').exists())

    def test_unban_reopens_registration(self):
        self._ban(reason='spam')
        self.client.force_login(self.mod)
        self.client.post(reverse('core:mod-unban-user', args=[self.target.id]))
        self.client.logout()
        block = BannedEmail.objects.get(email='spammer@example.com')
        self.assertIsNotNone(block.lifted)
        self.assertFalse(block.is_active())
        self.target.delete()
        self.assertEqual(self._signup('spammer@example.com').status_code, 401)

    def test_temporary_ban_lapses_the_email_block(self):
        self._ban(reason='spam', expires_days=7)
        block = BannedEmail.objects.get(email='spammer@example.com')
        self.assertIsNotNone(block.expires)
        # Fast-forward past expiry: the block goes inactive with the ban.
        block.expires = timezone.now() - timedelta(days=1)
        block.save(update_fields=['expires'])
        self.target.delete()
        self.assertEqual(self._signup('spammer@example.com').status_code, 401)

    def test_block_does_not_leak_via_password_reset(self):
        # clean_email is shared with the reset flow; the block must not fire
        # there, or a banned address becomes distinguishable (enumeration).
        self._ban(reason='spam')
        self.target.delete()
        response = self.client.post(
            '/_allauth/browser/v1/auth/password/request',
            {'email': 'spammer@example.com'},
            content_type='application/json',
        )
        # The uniform anonymous answer (see PasswordResetTests) — not the 400
        # the signup block raises, which would make the address distinguishable.
        self.assertEqual(response.status_code, 401)


class PasswordResetTests(ApiTestCase):
    """Reset is by emailed code, like signup verification — see settings."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            'drew', password='sturdy-passphrase-9'
        )
        EmailAddress.objects.create(
            user=self.user, email='drew@example.com', verified=True, primary=True
        )

    def request_reset(self, email='drew@example.com'):
        return self.client.post(
            '/_allauth/browser/v1/auth/password/request',
            {'email': email},
            content_type='application/json',
        )

    def emailed_code(self):
        return re.search(
            r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b', mail.outbox[0].body
        ).group(1)

    def test_reset_by_code_changes_the_password(self):
        # 401 is the expected answer: the code is out but the session is still
        # anonymous, exactly like the signup flow.
        self.assertEqual(self.request_reset().status_code, 401)
        self.assertEqual(len(mail.outbox), 1)
        response = self.client.post(
            '/_allauth/browser/v1/auth/password/reset',
            {'key': self.emailed_code(), 'password': 'brand-new-passphrase-4'},
            content_type='application/json',
        )
        # Still 401 — ACCOUNT_LOGIN_ON_PASSWORD_RESET is off, so the reset
        # lands without authenticating; the user logs in again afterwards.
        self.assertEqual(response.status_code, 401)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password('sturdy-passphrase-9'))
        self.assertTrue(self.user.check_password('brand-new-passphrase-4'))
        self.assertEqual(
            self.client.post(
                '/_allauth/browser/v1/auth/login',
                {'username': 'drew', 'password': 'brand-new-passphrase-4'},
                content_type='application/json',
            ).status_code,
            200,
        )

    def test_unknown_email_is_indistinguishable(self):
        # Enumeration resistance: an address with no account must answer the
        # same as one with, or the endpoint becomes an account oracle.
        known = self.request_reset()
        mail.outbox.clear()
        unknown = self.request_reset('nobody@example.com')
        self.assertEqual(unknown.status_code, known.status_code)
        self.assertFalse(
            User.objects.filter(email='nobody@example.com').exists()
        )

    def test_wrong_code_is_rejected(self):
        self.request_reset()
        response = self.client.post(
            '/_allauth/browser/v1/auth/password/reset',
            {'key': 'AAAA-BBBB', 'password': 'brand-new-passphrase-4'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('sturdy-passphrase-9'))

    def test_reset_without_a_request_conflicts(self):
        # The pending flow lives in the session, so a code can only be spent in
        # the browser that asked for it — 409, not 400.
        response = self.client.post(
            '/_allauth/browser/v1/auth/password/reset',
            {'key': 'AAAA-BBBB', 'password': 'brand-new-passphrase-4'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 409)


class SearchApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('drew', password='pw12345!')
        self.paris = _make_place(name='Paris', slug='paris')
        _publish(self.paris, self.user)
        PlaceName.objects.create(
            place=self.paris, name='Lutèce', language='fra'
        )

    def _get(self, q):
        return self.client.get(reverse('core:search'), {'q': q}).json()

    def test_matches_display_name(self):
        results = self._get('par')['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['slug'], 'paris')
        self.assertIsNone(results[0]['matched_name'])

    def test_matches_place_name_and_reports_alias(self):
        results = self._get('lutè')['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['slug'], 'paris')
        self.assertEqual(results[0]['matched_name'], 'Lutèce')

    def test_stub_places_excluded(self):
        _make_place(name='Paraguay Stub', slug='paraguay-stub')
        results = self._get('par')['results']
        self.assertEqual([r['slug'] for r in results], ['paris'])

    def test_short_or_missing_query_is_empty(self):
        self.assertEqual(self._get('p')['results'], [])
        response = self.client.get(reverse('core:search'))
        self.assertEqual(response.json()['results'], [])

    def test_no_match(self):
        self.assertEqual(self._get('zanzibar')['results'], [])

    def test_overlong_query_is_truncated(self):
        # A huge q must not become a huge ILIKE against every PlaceName. The
        # place below is named with 200 a's, so an untruncated 5000-char
        # query cannot match it and a truncated one must.
        long_named = _make_place(name='a' * 200, slug='long-named')
        _publish(long_named, self.user)
        results = self._get('a' * 5000)['results']
        self.assertEqual([r['slug'] for r in results], ['long-named'])

    def test_prefix_matches_rank_first(self):
        west = _make_place(name='West Paris', slug='west-paris')
        _publish(west, self.user)
        results = self._get('paris')['results']
        self.assertEqual(
            [r['slug'] for r in results], ['paris', 'west-paris']
        )

    def test_alias_match_not_duplicated(self):
        # display name AND an alias both match: one result row
        PlaceName.objects.create(
            place=self.paris, name='Parisius', language='lat'
        )
        results = self._get('paris')['results']
        self.assertEqual(len(results), 1)


class RandomApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('drew', password='pw12345!')

    def test_no_articles_yet(self):
        _make_place()  # stub only
        response = self.client.get(reverse('core:random'))
        self.assertIsNone(response.json()['place'])

    def test_returns_an_article_place(self):
        place = _make_place()
        place.label_point = Point(11.0, 51.0, srid=4326)
        place.bbox = Polygon.from_bbox((9.0, 49.0, 12.0, 52.0))
        place.save(update_fields=['label_point', 'bbox'])
        _publish(place, self.user)
        body = self.client.get(reverse('core:random')).json()['place']
        self.assertEqual(body['slug'], 'testville')
        # fly-to fields for search/deep links/random
        self.assertEqual(body['label_point'], [11.0, 51.0])
        self.assertEqual(body['bbox'], [9.0, 49.0, 12.0, 52.0])


class ModerationApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.place = _make_place()
        self.author = User.objects.create_user('drew', password='pw12345!')
        self.other = User.objects.create_user('sam', password='pw12345!')
        self.mod = User.objects.create_user(
            'mira', password='pw12345!', is_staff=True
        )

    # --- helpers -----------------------------------------------------
    def _thread_with_post(self):
        self.client.force_login(self.author)
        thread = self.client.post(
            reverse('core:talk', args=[self.place.slug]),
            {'title': 'Etymology dispute', 'body_md': 'Sources?'},
            content_type='application/json',
        ).json()['thread']
        self.client.logout()
        return thread, thread['posts'][0]

    def _revision(self):
        article = Article.objects.create(place=self.place)
        revision = Revision.objects.create(
            article=article, author=self.author, comment='draft',
            content=_content(),
        )
        article.current_revision = revision
        article.save(update_fields=['current_revision'])
        return revision

    def _report(self, target_type, target_id, reason='spam'):
        return self.client.post(
            reverse('core:report-create'),
            {'target_type': target_type, 'target_id': target_id,
             'reason': reason},
            content_type='application/json',
        )

    # --- roles -------------------------------------------------------
    def test_me_reports_moderator_flag(self):
        self.client.force_login(self.mod)
        self.assertTrue(
            self.client.get(reverse('core:me')).json()['user']['is_moderator']
        )
        self.client.force_login(self.other)
        self.assertFalse(
            self.client.get(reverse('core:me')).json()['user']['is_moderator']
        )

    # --- reporting ---------------------------------------------------
    def test_anonymous_cannot_report(self):
        post = self._thread_with_post()[1]
        self.assertEqual(
            self._report('talk_post', post['id']).status_code, 403
        )

    def test_report_talk_post_and_dedupe(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        self.assertEqual(
            self._report('talk_post', post['id']).status_code, 201
        )
        # same user, same target, still open -> idempotent (one row)
        self.assertEqual(
            self._report('talk_post', post['id']).status_code, 201
        )
        self.assertEqual(Report.objects.count(), 1)

    def test_report_revision(self):
        revision = self._revision()
        self.client.force_login(self.other)
        self.assertEqual(
            self._report('revision', revision.id).status_code, 201
        )
        report = Report.objects.get()
        self.assertEqual(report.revision_id, revision.id)
        self.assertEqual(report.status, Report.Status.OPEN)

    def test_report_stores_category(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        self.client.post(
            reverse('core:report-create'),
            {
                'target_type': 'talk_post',
                'target_id': post['id'],
                'category': 'harassment',
                'reason': 'rude',
            },
            content_type='application/json',
        )
        self.assertEqual(Report.objects.get().category, 'harassment')

    def test_report_accepts_copyright_category(self):
        # Copyvio is its own triage class, not "other": the serializer's
        # choices and the model's have to agree it exists.
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        response = self.client.post(
            reverse('core:report-create'),
            {
                'target_type': 'talk_post',
                'target_id': post['id'],
                'category': 'copyright',
                'reason': 'pasted from etymonline',
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            Report.objects.get().category, Report.Category.COPYRIGHT
        )

    def test_report_category_defaults_to_other(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        self._report('talk_post', post['id'])
        self.assertEqual(Report.objects.get().category, 'other')

    def test_report_rejects_unknown_category(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        response = self.client.post(
            reverse('core:report-create'),
            {
                'target_type': 'talk_post',
                'target_id': post['id'],
                'category': 'bogus',
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_report_unknown_target_404s(self):
        self.client.force_login(self.other)
        self.assertEqual(
            self._report('talk_post', 99999).status_code, 404
        )

    def test_cannot_report_own_talk_post(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.author)  # the post's author
        self.assertEqual(
            self._report('talk_post', post['id']).status_code, 400
        )
        self.assertEqual(Report.objects.count(), 0)

    def test_cannot_report_own_revision(self):
        revision = self._revision()  # authored by self.author
        self.client.force_login(self.author)
        self.assertEqual(
            self._report('revision', revision.id).status_code, 400
        )
        self.assertEqual(Report.objects.count(), 0)

    # --- mod queue ---------------------------------------------------
    def test_queue_requires_moderator(self):
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(reverse('core:mod-reports')).status_code, 403
        )

    def test_queue_lists_open_reports_with_context(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        self._report('talk_post', post['id'], reason='off topic')
        self.client.force_login(self.mod)
        reports = self.client.get(
            reverse('core:mod-reports')
        ).json()['reports']
        self.assertEqual(len(reports), 1)
        target = reports[0]['target']
        self.assertEqual(target['kind'], 'talk_post')
        self.assertEqual(target['author'], 'drew')
        self.assertEqual(target['excerpt'], 'Sources?')
        self.assertEqual(target['slug'], self.place.slug)
        self.assertEqual(reports[0]['reason'], 'off topic')

    def test_action_delete_softdeletes_post_and_resolves(self):
        thread, post = self._thread_with_post()
        self.client.force_login(self.other)
        self._report('talk_post', post['id'])
        report = Report.objects.get()
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-report-action', args=[report.id]),
            {'action': 'delete'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        report.refresh_from_db()
        self.assertEqual(report.status, Report.Status.RESOLVED)
        self.assertEqual(report.handled_by, self.mod)
        # post is a tombstone now
        threads = self.client.get(
            reverse('core:talk', args=[self.place.slug])
        ).json()['threads']
        tombstone = threads[0]['posts'][0]
        self.assertTrue(tombstone['deleted'])
        self.assertEqual(tombstone['body_md'], '')
        # and it left the open queue
        self.assertEqual(
            self.client.get(reverse('core:mod-reports')).json()['reports'], []
        )

    def test_delete_action_writes_audit_row(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        self._report('talk_post', post['id'])
        report = Report.objects.get()
        self.client.force_login(self.mod)
        self.client.post(
            reverse('core:mod-report-action', args=[report.id]),
            {'action': 'delete', 'reason': 'spam'},
            content_type='application/json',
        )
        entry = ModAction.objects.get()
        self.assertEqual(entry.action, ModAction.Action.DELETE_POST)
        self.assertEqual(entry.actor, self.mod)
        self.assertEqual(entry.target_user, self.author)
        self.assertEqual(entry.reason, 'spam')
        self.assertEqual(entry.talk_post_id, post['id'])

    def test_dismiss_action_writes_audit_row(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        self._report('talk_post', post['id'])
        report = Report.objects.get()
        self.client.force_login(self.mod)
        self.client.post(
            reverse('core:mod-report-action', args=[report.id]),
            {'action': 'dismiss'},
            content_type='application/json',
        )
        entry = ModAction.objects.get()
        self.assertEqual(entry.action, ModAction.Action.DISMISS_REPORT)
        self.assertEqual(entry.target_user, self.author)

    def test_mod_deleting_others_post_inline_is_audited(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.mod)
        self.client.delete(
            reverse('core:talk-post-delete', args=[post['id']])
        )
        entry = ModAction.objects.get()
        self.assertEqual(entry.action, ModAction.Action.DELETE_POST)
        self.assertEqual(entry.actor, self.mod)

    def test_author_deleting_own_post_is_not_audited(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.author)
        self.client.delete(
            reverse('core:talk-post-delete', args=[post['id']])
        )
        self.assertEqual(ModAction.objects.count(), 0)

    def test_action_dismiss_keeps_post(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        self._report('talk_post', post['id'])
        report = Report.objects.get()
        self.client.force_login(self.mod)
        self.client.post(
            reverse('core:mod-report-action', args=[report.id]),
            {'action': 'dismiss'},
            content_type='application/json',
        )
        report.refresh_from_db()
        self.assertEqual(report.status, Report.Status.DISMISSED)
        self.assertIsNone(TalkPost.objects.get(id=post['id']).deleted)

    def test_action_delete_on_revision_report_rejected(self):
        revision = self._revision()
        self.client.force_login(self.other)
        self._report('revision', revision.id)
        report = Report.objects.get()
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-report-action', args=[report.id]),
            {'action': 'delete'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    # --- revision suppression ---------------------------------------
    def _older_revision(self):
        """An older, non-current revision (the current one can't be
        suppressed) plus the article it belongs to."""
        old = self._revision()  # this became current
        article = old.article
        new = Revision.objects.create(
            article=article, author=self.author, comment='newer',
            content=_content(),
        )
        article.current_revision = new
        article.save(update_fields=['current_revision'])
        return old

    def test_action_suppress_hides_revision_and_resolves(self):
        old = self._older_revision()
        self.client.force_login(self.other)
        self._report('revision', old.id)
        report = Report.objects.get()
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-report-action', args=[report.id]),
            {'action': 'suppress'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        old.refresh_from_db()
        self.assertIsNotNone(old.suppressed)
        self.assertEqual(old.suppressed_by, self.mod)
        report.refresh_from_db()
        self.assertEqual(report.status, Report.Status.RESOLVED)

    def test_suppressed_revision_hidden_from_public_history(self):
        old = self._older_revision()
        old.suppressed = timezone.now()
        old.suppressed_by = self.mod
        old.save(update_fields=['suppressed', 'suppressed_by'])
        url = reverse('core:revision-list', args=[self.place.slug])
        # anonymous: suppressed row is gone
        ids = [r['id'] for r in self.client.get(url).json()['revisions']]
        self.assertNotIn(old.id, ids)
        # moderator: still there, flagged
        self.client.force_login(self.mod)
        rows = self.client.get(url).json()['revisions']
        row = next(r for r in rows if r['id'] == old.id)
        self.assertTrue(row['suppressed'])

    def test_suppressed_revision_detail_404_for_public(self):
        old = self._older_revision()
        old.suppressed = timezone.now()
        old.save(update_fields=['suppressed'])
        url = reverse(
            'core:revision-detail', args=[self.place.slug, old.id]
        )
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.mod)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_cannot_suppress_current_revision(self):
        revision = self._revision()  # is current
        self.client.force_login(self.other)
        self._report('revision', revision.id)
        report = Report.objects.get()
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-report-action', args=[report.id]),
            {'action': 'suppress'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        revision.refresh_from_db()
        self.assertIsNone(revision.suppressed)

    def test_mod_restores_suppressed_revision(self):
        old = self._older_revision()
        old.suppressed = timezone.now()
        old.suppressed_by = self.mod
        old.save(update_fields=['suppressed', 'suppressed_by'])
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-revision-restore', args=[old.id])
        )
        self.assertEqual(response.status_code, 200)
        old.refresh_from_db()
        self.assertIsNone(old.suppressed)
        self.assertIsNone(old.suppressed_by)

    def test_revision_restore_requires_moderator(self):
        old = self._older_revision()
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.post(
                reverse('core:mod-revision-restore', args=[old.id])
            ).status_code,
            403,
        )

    def test_mod_restores_deleted_talk_post(self):
        post = self._thread_with_post()[1]
        tp = TalkPost.objects.get(id=post['id'])
        tp.deleted = timezone.now()
        tp.deleted_by = self.mod
        tp.save(update_fields=['deleted', 'deleted_by'])
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-talk-post-restore', args=[post['id']])
        )
        self.assertEqual(response.status_code, 200)
        tp.refresh_from_db()
        self.assertIsNone(tp.deleted)

    # --- soft delete of talk content --------------------------------
    def test_author_deletes_own_post(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.author)
        response = self.client.delete(
            reverse('core:talk-post-delete', args=[post['id']])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(TalkPost.objects.get(id=post['id']).deleted)

    def test_stranger_cannot_delete_post(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.delete(
                reverse('core:talk-post-delete', args=[post['id']])
            ).status_code,
            403,
        )

    def test_moderator_deletes_others_post(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.mod)
        self.assertEqual(
            self.client.delete(
                reverse('core:talk-post-delete', args=[post['id']])
            ).status_code,
            200,
        )

    def test_cannot_edit_deleted_post(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.author)
        self.client.delete(
            reverse('core:talk-post-delete', args=[post['id']])
        )
        response = self.client.put(
            reverse('core:talk-post-edit', args=[post['id']]),
            {'body_md': 'come back'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_moderator_deletes_thread_hides_it(self):
        thread = self._thread_with_post()[0]
        self.client.force_login(self.mod)
        response = self.client.delete(
            reverse('core:talk-thread-delete', args=[thread['id']])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(
                reverse('core:talk', args=[self.place.slug])
            ).json()['threads'],
            [],
        )

    def test_non_moderator_cannot_delete_thread(self):
        thread = self._thread_with_post()[0]
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.delete(
                reverse('core:talk-thread-delete', args=[thread['id']])
            ).status_code,
            403,
        )


class BanEnforcementTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.place = _make_place()
        self.user = User.objects.create_user('drew', password='pw12345!')
        self.mod = User.objects.create_user(
            'mira', password='pw12345!', is_staff=True
        )

    def _ban(self, expires=None, reason='spamming'):
        return Ban.objects.create(
            user=self.user, created_by=self.mod, reason=reason,
            expires=expires,
        )

    def _edit(self):
        return self.client.put(
            reverse('core:article-edit', args=[self.place.slug]),
            {'content': _content(), 'comment': 'x'},
            content_type='application/json',
        )

    def _new_thread(self):
        return self.client.post(
            reverse('core:talk', args=[self.place.slug]),
            {'title': 'Hi', 'body_md': 'there'},
            content_type='application/json',
        )

    def test_active_ban_blocks_editing_with_message(self):
        self._ban(reason='spamming')
        self.client.force_login(self.user)
        response = self._edit()
        self.assertEqual(response.status_code, 403)
        self.assertIn('suspended', response.json()['error'])
        self.assertIn('spamming', response.json()['error'])

    def test_active_ban_blocks_new_thread(self):
        self._ban()
        self.client.force_login(self.user)
        self.assertEqual(self._new_thread().status_code, 403)

    def test_active_ban_blocks_reporting(self):
        # something to report
        self.client.force_login(self.mod)
        thread = self._new_thread().json()['thread']
        self.client.logout()
        self._ban()
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('core:report-create'),
            {'target_type': 'talk_post', 'target_id': thread['posts'][0]['id']},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_ban_does_not_block_reading(self):
        self._ban()
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(
                reverse('core:talk', args=[self.place.slug])
            ).status_code,
            200,
        )

    def test_expired_ban_does_not_block(self):
        self._ban(expires=timezone.now() - timedelta(days=1))
        self.client.force_login(self.user)
        self.assertEqual(self._edit().status_code, 200)

    def test_lifted_ban_does_not_block(self):
        ban = self._ban()
        ban.lifted = timezone.now()
        ban.save(update_fields=['lifted'])
        self.client.force_login(self.user)
        self.assertEqual(self._edit().status_code, 200)

    def test_me_reports_suspension(self):
        self._ban(reason='abuse')
        self.client.force_login(self.user)
        data = self.client.get(reverse('core:me')).json()['user']
        self.assertIsNotNone(data['suspended'])
        self.assertIn('abuse', data['suspended'])

    def test_me_not_suspended_when_clean(self):
        self.client.force_login(self.user)
        data = self.client.get(reverse('core:me')).json()['user']
        self.assertIsNone(data['suspended'])


class DashboardApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.place = _make_place()
        self.author = User.objects.create_user('drew', password='pw12345!')
        self.reporter = User.objects.create_user('sam', password='pw12345!')
        self.mod = User.objects.create_user(
            'mira', password='pw12345!', is_staff=True
        )
        self.admin = User.objects.create_superuser(
            'root', password='pw12345!'
        )

    def _post_by(self, user, body='hello'):
        thread = TalkThread.objects.create(place=self.place, title='t')
        return TalkPost.objects.create(
            thread=thread, author=user, body_md=body
        )

    def _report_post(self, post, reporter, status_=Report.Status.OPEN):
        return Report.objects.create(
            talk_post=post, reporter=reporter, status=status_,
            category=Report.Category.SPAM,
        )

    # --- users list --------------------------------------------------
    def test_users_list_requires_moderator(self):
        self.client.force_login(self.reporter)
        self.assertEqual(
            self.client.get(reverse('core:mod-users')).status_code, 403
        )

    def test_users_list_includes_reported_author(self):
        post = self._post_by(self.author)
        self._report_post(post, self.reporter)
        self.client.force_login(self.mod)
        rows = self.client.get(reverse('core:mod-users')).json()['users']
        row = next(r for r in rows if r['username'] == 'drew')
        self.assertEqual(row['reports_open'], 1)
        self.assertEqual(row['reports_total'], 1)
        self.assertFalse(row['banned'])

    def test_users_list_includes_removed_content_author(self):
        post = self._post_by(self.author)
        post.deleted = timezone.now()
        post.save(update_fields=['deleted'])
        self.client.force_login(self.mod)
        rows = self.client.get(reverse('core:mod-users')).json()['users']
        row = next(r for r in rows if r['username'] == 'drew')
        self.assertEqual(row['removed_count'], 1)

    # --- the ?all=1 roster -------------------------------------------
    def test_users_list_excludes_clean_users_by_default(self):
        User.objects.create_user('quiet', password='pw12345!')
        self.client.force_login(self.admin)
        rows = self.client.get(reverse('core:mod-users')).json()['users']
        self.assertNotIn('quiet', [r['username'] for r in rows])

    def test_all_users_includes_clean_users_for_superuser(self):
        User.objects.create_user('quiet', password='pw12345!')
        self.client.force_login(self.admin)
        data = self.client.get(reverse('core:mod-users'), {'all': '1'}).json()
        names = [r['username'] for r in data['users']]
        self.assertIn('quiet', names)
        self.assertFalse(data['truncated'])

    def test_all_users_ignored_for_plain_moderator(self):
        User.objects.create_user('quiet', password='pw12345!')
        self.client.force_login(self.mod)
        rows = self.client.get(
            reverse('core:mod-users'), {'all': '1'}
        ).json()['users']
        self.assertNotIn('quiet', [r['username'] for r in rows])

    def test_all_users_sorts_reported_first_then_alphabetical(self):
        # 'aaron' is clean but sorts first alphabetically; the reported
        # author must still outrank him.
        User.objects.create_user('aaron', password='pw12345!')
        self._report_post(self._post_by(self.author), self.reporter)
        self.client.force_login(self.admin)
        rows = self.client.get(
            reverse('core:mod-users'), {'all': '1'}
        ).json()['users']
        names = [r['username'] for r in rows]
        self.assertEqual(names[0], 'drew')  # the reported author
        tail = names[1:]
        self.assertEqual(tail, sorted(tail))

    @patch.object(dashboard, 'ALL_USERS_CAP', 2)
    def test_all_users_truncates_at_cap(self):
        for i in range(4):
            User.objects.create_user(f'extra{i}', password='pw12345!')
        self.client.force_login(self.admin)
        data = self.client.get(reverse('core:mod-users'), {'all': '1'}).json()
        self.assertTrue(data['truncated'])
        self.assertEqual(len(data['users']), 2)

    # --- user detail -------------------------------------------------
    def test_user_detail_shows_removed_post_body(self):
        post = self._post_by(self.author, body='secret text')
        post.deleted = timezone.now()
        post.save(update_fields=['deleted'])
        self.client.force_login(self.mod)
        data = self.client.get(
            reverse('core:mod-user-detail', args=[self.author.id])
        ).json()
        tp = data['talk_posts'][0]
        self.assertTrue(tp['deleted'])
        self.assertEqual(tp['body_md'], 'secret text')  # visible to mods

    # --- banning -----------------------------------------------------
    def test_mod_bans_regular_user(self):
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-ban-user', args=[self.author.id]),
            {'reason': 'spam'}, content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(Ban.objects.get(user=self.author).is_active())
        self.assertEqual(
            ModAction.objects.filter(
                action=ModAction.Action.BAN_USER
            ).count(),
            1,
        )

    def test_temporary_ban_sets_expiry(self):
        self.client.force_login(self.mod)
        self.client.post(
            reverse('core:mod-ban-user', args=[self.author.id]),
            {'reason': 'x', 'expires_days': 7},
            content_type='application/json',
        )
        self.assertIsNotNone(Ban.objects.get(user=self.author).expires)

    def test_ban_with_remove_content_removes_posts(self):
        post = self._post_by(self.author)
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-ban-user', args=[self.author.id]),
            {'reason': 'x', 'remove_content': True},
            content_type='application/json',
        )
        self.assertEqual(response.json()['removed_content'], 1)
        post.refresh_from_db()
        self.assertIsNotNone(post.deleted)

    def test_ban_rejects_malformed_body(self):
        # These all used to reach the model layer and 500: a JSON object
        # sliced with [:500] raises KeyError, and a huge day count overflows
        # timedelta.
        self.client.force_login(self.mod)
        for body in (
            {'reason': {'a': 1}},
            {'reason': ['x']},
            {'expires_days': 10 ** 9},
            {'expires_days': MAX_BAN_DAYS + 1},
            {'expires_days': -1},
            {'expires_days': 'abc'},
            {'remove_content': {'a': 1}},
        ):
            with self.subTest(body=body):
                response = self.client.post(
                    reverse('core:mod-ban-user', args=[self.author.id]),
                    body, content_type='application/json',
                )
                self.assertEqual(response.status_code, 400)
        self.assertFalse(Ban.objects.filter(user=self.author).exists())

    def test_ban_accepts_the_longest_allowed_duration(self):
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-ban-user', args=[self.author.id]),
            {'reason': 'x', 'expires_days': MAX_BAN_DAYS},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertIsNotNone(Ban.objects.get(user=self.author).expires)

    def test_ban_treats_explicit_nulls_as_permanent(self):
        # The hand-rolled parsing this replaced coerced with `or`, so a client
        # sending nulls must not start getting 400s.
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-ban-user', args=[self.author.id]),
            {'reason': None, 'expires_days': None},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        ban = Ban.objects.get(user=self.author)
        self.assertIsNone(ban.expires)
        self.assertEqual(ban.reason, '')

    def test_failed_ban_rolls_back_completely(self):
        # A ban whose email blocklist didn't get written is one the target
        # can walk around by re-registering, so nothing may survive a
        # partial failure — not the Ban row, not the audit entry.
        self.client.force_login(self.mod)
        with patch(
            'core.dashboard.block_user_emails', side_effect=RuntimeError('db')
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('core:mod-ban-user', args=[self.author.id]),
                    {'reason': 'spam'}, content_type='application/json',
                )
        self.assertFalse(Ban.objects.filter(user=self.author).exists())
        self.assertFalse(
            ModAction.objects.filter(
                action=ModAction.Action.BAN_USER
            ).exists()
        )

    def test_failed_unban_rolls_back_completely(self):
        ban = Ban.objects.create(user=self.author, created_by=self.mod)
        self.client.force_login(self.mod)
        with patch(
            'core.dashboard.lift_user_email_blocks',
            side_effect=RuntimeError('db'),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('core:mod-unban-user', args=[self.author.id]),
                    {}, content_type='application/json',
                )
        ban.refresh_from_db()
        self.assertIsNone(ban.lifted)
        self.assertTrue(ban.is_active())

    def test_mod_cannot_ban_moderator(self):
        other_mod = User.objects.create_user(
            'mira2', password='pw12345!', is_staff=True
        )
        self.client.force_login(self.mod)
        self.assertEqual(
            self.client.post(
                reverse('core:mod-ban-user', args=[other_mod.id]),
                {}, content_type='application/json',
            ).status_code,
            403,
        )

    def test_superuser_can_ban_moderator(self):
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.post(
                reverse('core:mod-ban-user', args=[self.mod.id]),
                {'reason': 'rogue'}, content_type='application/json',
            ).status_code,
            201,
        )

    def test_nobody_can_ban_superuser(self):
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.post(
                reverse('core:mod-ban-user', args=[self.admin.id]),
                {}, content_type='application/json',
            ).status_code,
            403,
        )

    def test_unban_lifts_active_ban(self):
        Ban.objects.create(user=self.author, created_by=self.mod)
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-unban-user', args=[self.author.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['lifted'])
        self.assertFalse(
            any(b.is_active() for b in self.author.bans.all())
        )

    # --- roles -------------------------------------------------------
    def _set_role(self, target, role):
        return self.client.post(
            reverse('core:mod-set-role', args=[target.id]),
            {'role': role}, content_type='application/json',
        )

    def test_superuser_promotes_user_to_moderator(self):
        self.client.force_login(self.admin)
        response = self._set_role(self.author, 'moderator')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['role'], 'moderator')
        self.author.refresh_from_db()
        self.assertTrue(self.author.is_staff)
        self.assertEqual(
            ModAction.objects.filter(
                action=ModAction.Action.PROMOTE_MOD, target_user=self.author
            ).count(),
            1,
        )

    def test_superuser_demotes_moderator(self):
        self.client.force_login(self.admin)
        response = self._set_role(self.mod, 'user')
        self.assertEqual(response.status_code, 200)
        self.mod.refresh_from_db()
        self.assertFalse(self.mod.is_staff)
        self.assertEqual(
            ModAction.objects.filter(
                action=ModAction.Action.DEMOTE_MOD, target_user=self.mod
            ).count(),
            1,
        )

    def test_moderator_cannot_change_roles(self):
        self.client.force_login(self.mod)
        self.assertEqual(self._set_role(self.author, 'moderator').status_code, 403)
        self.author.refresh_from_db()
        self.assertFalse(self.author.is_staff)

    def test_nobody_can_change_own_role(self):
        self.client.force_login(self.admin)
        self.assertEqual(self._set_role(self.admin, 'user').status_code, 403)

    def test_superuser_role_cannot_be_changed(self):
        other_admin = User.objects.create_superuser(
            'root2', password='pw12345!'
        )
        self.client.force_login(self.admin)
        self.assertEqual(self._set_role(other_admin, 'user').status_code, 403)
        other_admin.refresh_from_db()
        self.assertTrue(other_admin.is_superuser)

    def test_invalid_role_rejected(self):
        self.client.force_login(self.admin)
        self.assertEqual(self._set_role(self.author, 'wizard').status_code, 400)

    def test_role_change_rejects_malformed_reason(self):
        # The audit note used to be sliced with [:500], which raises on a
        # JSON object.
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('core:mod-set-role', args=[self.author.id]),
            {'role': 'moderator', 'reason': {'a': 1}},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.author.refresh_from_db()
        self.assertFalse(self.author.is_staff)

    def test_role_change_accepts_a_null_reason(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('core:mod-set-role', args=[self.author.id]),
            {'role': 'moderator', 'reason': None},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            ModAction.objects.get(
                action=ModAction.Action.PROMOTE_MOD
            ).reason,
            '',
        )

    def test_role_change_by_non_admin_is_forbidden_before_validation(self):
        # Authorization must be decided before the body is parsed, so a
        # moderator poking at the endpoint learns nothing from the response.
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-set-role', args=[self.author.id]),
            {'role': 'nonsense'}, content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_repeat_promotion_does_not_duplicate_audit_rows(self):
        self.client.force_login(self.admin)
        self._set_role(self.author, 'moderator')
        self._set_role(self.author, 'moderator')
        self.assertEqual(
            ModAction.objects.filter(
                action=ModAction.Action.PROMOTE_MOD
            ).count(),
            1,
        )

    def test_anonymous_cannot_change_roles(self):
        self.assertIn(self._set_role(self.author, 'moderator').status_code,
                      (401, 403))

    def test_user_detail_reports_role_authority(self):
        self.client.force_login(self.admin)
        data = self.client.get(
            reverse('core:mod-user-detail', args=[self.author.id])
        ).json()
        self.assertTrue(data['can_set_role'])
        self.client.force_login(self.mod)
        data = self.client.get(
            reverse('core:mod-user-detail', args=[self.author.id])
        ).json()
        self.assertFalse(data['can_set_role'])

    # --- reporters ---------------------------------------------------
    def test_reporters_ranked_by_dismissed(self):
        p1 = self._post_by(self.author)
        p2 = self._post_by(self.author)
        self._report_post(p1, self.reporter, Report.Status.DISMISSED)
        self._report_post(p2, self.reporter, Report.Status.DISMISSED)
        good = User.objects.create_user('good', password='pw12345!')
        self._report_post(
            self._post_by(self.author), good, Report.Status.RESOLVED
        )
        self.client.force_login(self.mod)
        rows = self.client.get(
            reverse('core:mod-reporters')
        ).json()['reporters']
        self.assertEqual(rows[0]['username'], 'sam')
        self.assertEqual(rows[0]['dismissed'], 2)


class ProtectionApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.place = _make_place()
        self.author = User.objects.create_user('drew', password='pw12345!')
        self.mod = User.objects.create_user(
            'mira', password='pw12345!', is_staff=True
        )

    def _edit(self, comment='edit'):
        return self.client.put(
            reverse('core:article-edit', args=[self.place.slug]),
            {'content': _content(), 'comment': comment},
            content_type='application/json',
        )

    def _set_protection(self, level):
        return self.client.post(
            reverse('core:article-protection', args=[self.place.slug]),
            {'protection_level': level},
            content_type='application/json',
        )

    def test_non_moderator_cannot_set_protection(self):
        self.client.force_login(self.author)
        self.assertEqual(self._set_protection('admin').status_code, 403)

    def test_moderator_sets_protection_on_stub(self):
        self.client.force_login(self.mod)
        response = self._set_protection('admin')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['protection_level'], 'admin')
        # place detail now surfaces the level even without a revision
        detail = self.client.get(
            reverse('core:place-detail', args=[self.place.slug])
        ).json()
        self.assertEqual(detail['protection_level'], 'admin')
        self.assertIsNone(detail['article'])

    def test_admin_protection_blocks_non_moderator_edit(self):
        self.client.force_login(self.author)
        self.assertEqual(self._edit().status_code, 200)  # editable at none
        self.client.force_login(self.mod)
        self._set_protection('admin')
        self.client.force_login(self.author)
        self.assertEqual(self._edit(comment='again').status_code, 403)
        # a moderator can still edit
        self.client.force_login(self.mod)
        self.assertEqual(self._edit(comment='mod edit').status_code, 200)

    def test_registered_protection_still_lets_users_edit(self):
        self.client.force_login(self.mod)
        self._set_protection('registered')
        self.client.force_login(self.author)
        self.assertEqual(self._edit().status_code, 200)

    def test_admin_protection_blocks_non_moderator_revert(self):
        self.client.force_login(self.author)
        self._edit(comment='first')
        first = Revision.objects.order_by('id').first()
        self._edit(comment='second')
        self.client.force_login(self.mod)
        self._set_protection('admin')
        self.client.force_login(self.author)
        response = self.client.post(
            reverse('core:article-revert', args=[self.place.slug]),
            {'revision_id': first.id},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)


class ThrottleApiTests(ApiTestCase):
    def test_report_endpoint_throttles_after_limit(self):
        # 'report' scope is 15/min; the 16th call in a test gets 429.
        place = _make_place()
        author = User.objects.create_user('drew', password='pw12345!')
        reporter = User.objects.create_user('sam', password='pw12345!')
        article = Article.objects.create(place=place)
        revision = Revision.objects.create(
            article=article, author=author, comment='', content=_content()
        )
        self.client.force_login(reporter)
        seen_429 = False
        for i in range(20):
            response = self.client.post(
                reverse('core:report-create'),
                {'target_type': 'revision', 'target_id': revision.id,
                 'reason': f'n{i}'},
                content_type='application/json',
            )
            if response.status_code == 429:
                seen_429 = True
                break
        self.assertTrue(seen_429, 'report endpoint never throttled')

    def test_thread_creation_throttles_after_limit(self):
        # 'talk' scope is 40/min and covers new threads, not just replies.
        place = _make_place()
        user = User.objects.create_user('drew', password='pw12345!')
        self.client.force_login(user)
        seen_429 = False
        for i in range(60):
            response = self.client.post(
                reverse('core:talk', args=[place.slug]),
                {'title': f'Topic {i}', 'body_md': 'Sources?'},
                content_type='application/json',
            )
            if response.status_code == 429:
                seen_429 = True
                break
        self.assertTrue(seen_429, 'thread creation never throttled')

    def test_reading_threads_is_not_billed_to_the_write_bucket(self):
        # The list and the create share a URL; reads must not exhaust the
        # 40/min talk budget (they answer to the anon/user rates instead).
        place = _make_place()
        url = reverse('core:talk', args=[place.slug])
        for _ in range(60):
            self.assertEqual(self.client.get(url).status_code, 200)
        user = User.objects.create_user('drew', password='pw12345!')
        self.client.force_login(user)
        response = self.client.post(
            url,
            {'title': 'Still allowed', 'body_md': 'Sources?'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)


SPA_INDEX_FIXTURE = (
    '<!doctype html><html><head><meta charset="UTF-8" />'
    '<title>Toponymia</title>\n<!--seo-->\n'
    '<script src="/assets/index-TEST.js"></script></head>'
    '<body><div id="root"></div></body></html>'
)


class SpaTests(TestCase):
    """core/spa.py: the served shell, per-place SEO meta, sitemap, robots.
    A fixture dist dir stands in for the Vite build so the suite doesn't
    depend on `npm run build` having run."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dist = Path(tempfile.mkdtemp(prefix='toponymia-dist-'))
        (cls.dist / 'index.html').write_text(SPA_INDEX_FIXTURE)
        cls._settings = override_settings(WEB_DIST=cls.dist)
        cls._settings.enable()

    @classmethod
    def tearDownClass(cls):
        cls._settings.disable()
        shutil.rmtree(cls.dist)
        super().tearDownClass()

    def _article_place(self, name='Testville', slug='testville', content=None):
        place = _make_place(name=name, slug=slug)
        author = User.objects.create_user(f'author-{slug}', password='pw12345!')
        save_edit(place, author, content or _content(), 'seed')
        return place

    def test_root_serves_shell_with_default_meta(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<title>Toponymia</title>', html)
        self.assertIn('<div id="root">', html)
        self.assertIn('property="og:site_name" content="Toponymia"', html)
        self.assertIn(
            '<link rel="canonical" href="http://testserver/" />', html
        )
        self.assertNotIn('<!--seo-->', html)

    def test_place_with_article_meta(self):
        self._article_place()
        response = self.client.get('/place/testville')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<title>Testville – Toponymia</title>', html)
        # Markdown stripped from the etymology excerpt.
        self.assertIn('content="Testville: test + -ville"', html)
        self.assertIn('property="og:type" content="article"', html)
        self.assertIn(
            'href="http://testserver/place/testville"', html
        )

    def test_stub_place_meta(self):
        _make_place(name='Stubton', slug='stubton')
        response = self.client.get('/place/stubton')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<title>Stubton – Toponymia</title>', html)
        self.assertIn('property="og:type" content="website"', html)
        self.assertIn('What does the name', html)

    def test_meta_values_escaped(self):
        self._article_place(name='A & B', slug='a-b')
        html = self.client.get('/place/a-b').content.decode()
        self.assertIn('<title>A &amp; B – Toponymia</title>', html)
        self.assertNotIn('<title>A & B', html)

    def test_unknown_slug_is_404_shell(self):
        response = self.client.get('/place/nowhere')
        self.assertEqual(response.status_code, 404)
        self.assertIn('<div id="root">', response.content.decode())

    def test_unknown_path_is_404_shell(self):
        response = self.client.get('/no/such/page')
        self.assertEqual(response.status_code, 404)
        self.assertIn('<div id="root">', response.content.decode())

    def test_missing_build_returns_503(self):
        with override_settings(WEB_DIST=self.dist / 'nope'):
            response = self.client.get('/')
        self.assertEqual(response.status_code, 503)
        self.assertIn('npm run build', response.content.decode())

    def test_sitemap_lists_article_places_only(self):
        self._article_place()
        _make_place(name='Stubton', slug='stubton')
        response = self.client.get('/sitemap.xml')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/xml')
        xml = _sitemap_xml(response)
        self.assertIn('<loc>http://testserver/</loc>', xml)
        self.assertIn('<loc>http://testserver/place/testville</loc>', xml)
        self.assertIn('<lastmod>', xml)
        self.assertNotIn('stubton', xml)

    def test_robots(self):
        response = self.client.get('/robots.txt')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Disallow: /api/', body)
        self.assertIn('Sitemap: http://testserver/sitemap.xml', body)


class ArticleDeleteTests(ApiTestCase):
    """Admin-only whole-article soft delete."""

    def setUp(self):
        super().setUp()
        self.place = _make_place()
        self.author = User.objects.create_user('author', password='x')
        self.mod = User.objects.create_user('mod', password='x', is_staff=True)
        self.admin = User.objects.create_superuser('admin', password='x')
        self.revision = save_edit(
            self.place, self.author, _content(), 'first'
        )
        self.article = self.revision.article

    def _delete(self, reason=''):
        return self.client.post(
            reverse('core:article-delete', args=[self.place.slug]),
            {'reason': reason},
            content_type='application/json',
        )

    def _mark_deleted(self):
        self.article.deleted = timezone.now()
        self.article.deleted_by = self.admin
        self.article.save(update_fields=['deleted', 'deleted_by'])

    def test_admin_deletes_article(self):
        self.client.force_login(self.admin)
        response = self._delete(reason='test article')
        self.assertEqual(response.status_code, 200)
        self.article.refresh_from_db()
        self.assertIsNotNone(self.article.deleted)
        self.assertEqual(self.article.deleted_by, self.admin)

    def test_delete_writes_audit_row_naming_the_author(self):
        self.client.force_login(self.admin)
        self._delete(reason='throwaway')
        entry = ModAction.objects.get(action=ModAction.Action.DELETE_ARTICLE)
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.target_user, self.author)
        self.assertEqual(entry.reason, 'throwaway')
        self.assertEqual(entry.article, self.article)

    def test_moderator_cannot_delete_article(self):
        """Deletion is admin-only — a moderator gets 403."""
        self.client.force_login(self.mod)
        self.assertEqual(self._delete().status_code, 403)
        self.article.refresh_from_db()
        self.assertIsNone(self.article.deleted)

    def test_author_cannot_delete_own_article(self):
        self.client.force_login(self.author)
        self.assertEqual(self._delete().status_code, 403)

    def test_anonymous_cannot_delete_article(self):
        self.assertIn(self._delete().status_code, (401, 403))

    def test_delete_rejects_place_without_article(self):
        other = _make_place(name='Stubville', slug='stubville')
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('core:article-delete', args=[other.slug]),
            {'reason': ''},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_delete_is_idempotent(self):
        """Re-deleting must not clobber the original actor/timestamp."""
        self.client.force_login(self.admin)
        self._delete()
        self.article.refresh_from_db()
        first = self.article.deleted
        self._delete()
        self.article.refresh_from_db()
        self.assertEqual(self.article.deleted, first)

    def test_deleted_article_reads_as_stub_to_public(self):
        self._mark_deleted()
        body = self.client.get(
            reverse('core:place-detail', args=[self.place.slug])
        ).json()
        self.assertIsNone(body['article'])
        # The public can't even tell a deleted article from an unwritten one.
        self.assertIsNone(body['deleted'])

    def test_deleted_article_visible_to_admin_with_banner(self):
        self._mark_deleted()
        self.client.force_login(self.admin)
        body = self.client.get(
            reverse('core:place-detail', args=[self.place.slug])
        ).json()
        self.assertIsNotNone(body['article'])
        self.assertEqual(body['deleted']['by'], 'admin')

    def test_deleted_article_hidden_from_moderator_too(self):
        """Delete is admin-scoped: a mod sees the stub like anyone else."""
        self._mark_deleted()
        self.client.force_login(self.mod)
        body = self.client.get(
            reverse('core:place-detail', args=[self.place.slug])
        ).json()
        self.assertIsNone(body['article'])
        self.assertIsNone(body['deleted'])

    def test_deleted_article_history_is_empty_for_public(self):
        """The content must not stay readable through the History tab —
        that would make the deletion meaningless."""
        self._mark_deleted()
        rows = self.client.get(
            reverse('core:revision-list', args=[self.place.slug])
        ).json()['revisions']
        self.assertEqual(rows, [])
        self.client.force_login(self.admin)
        rows = self.client.get(
            reverse('core:revision-list', args=[self.place.slug])
        ).json()['revisions']
        self.assertEqual(len(rows), 1)

    def test_deleted_article_revision_detail_404s_for_public(self):
        self._mark_deleted()
        url = reverse(
            'core:revision-detail', args=[self.place.slug, self.revision.id]
        )
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_deleted_article_drops_out_of_search(self):
        self._mark_deleted()
        results = self.client.get(
            reverse('core:search'), {'q': 'Testville'}
        ).json()['results']
        self.assertEqual(results, [])

    def test_deleted_article_drops_out_of_random(self):
        self._mark_deleted()
        body = self.client.get(reverse('core:random')).json()
        self.assertIsNone(body['place'])

    def test_deleted_article_drops_out_of_highlights(self):
        self._mark_deleted()
        body = self.client.get(
            reverse('core:highlights'), {'bbox': '0,40,20,60'}
        ).json()
        self.assertEqual(body['features'], [])

    def test_admin_restores_article(self):
        self._mark_deleted()
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('core:article-restore', args=[self.place.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.article.refresh_from_db()
        self.assertIsNone(self.article.deleted)
        self.assertIsNone(self.article.deleted_by)
        entry = ModAction.objects.get(action=ModAction.Action.RESTORE_ARTICLE)
        self.assertEqual(entry.actor, self.admin)

    def test_restore_requires_admin(self):
        self._mark_deleted()
        self.client.force_login(self.mod)
        self.assertEqual(
            self.client.post(
                reverse('core:article-restore', args=[self.place.slug])
            ).status_code,
            403,
        )

    def test_write_clears_the_deletion(self):
        """A new write IS the restore — which is why
        "restore an article someone has since rewritten" can't happen."""
        self._mark_deleted()
        save_edit(self.place, self.author, _content(), 'rewritten')
        self.article.refresh_from_db()
        self.assertIsNone(self.article.deleted)
        self.assertIsNone(self.article.deleted_by)

    def test_write_on_deleted_article_preserves_earlier_revisions(self):
        """Nothing is ever destroyed: the old content becomes history."""
        self._mark_deleted()
        new = save_edit(self.place, self.author, _content(), 'rewritten')
        self.article.refresh_from_db()
        self.assertEqual(self.article.current_revision_id, new.id)
        ids = set(self.article.revisions.values_list('id', flat=True))
        self.assertEqual(ids, {self.revision.id, new.id})

    def test_suppressed_revision_stays_hidden_through_a_rewrite(self):
        """Delete is not a content remedy — suppression is, and its flag is
        independent, so it survives the article coming back."""
        self.revision.suppressed = timezone.now()
        self.revision.suppressed_by = self.mod
        self.revision.save(update_fields=['suppressed', 'suppressed_by'])
        self._mark_deleted()
        save_edit(self.place, self.author, _content(), 'rewritten')
        self.revision.refresh_from_db()
        self.assertIsNotNone(self.revision.suppressed)
        ids = [
            r['id'] for r in self.client.get(
                reverse('core:revision-list', args=[self.place.slug])
            ).json()['revisions']
        ]
        self.assertNotIn(self.revision.id, ids)


class RevertAuditTests(ApiTestCase):
    """Revert used to leave no audit trail at all — a rogue mod could blank
    the wiki through it silently."""

    def setUp(self):
        super().setUp()
        self.place = _make_place()
        self.author = User.objects.create_user('author', password='x')
        self.mod = User.objects.create_user('mod', password='x', is_staff=True)
        self.old = save_edit(self.place, self.author, _content(), 'first')
        save_edit(
            self.place, self.author, _content(names=[{
                'name': 'Testville', 'language': 'eng',
                'etymology_md': 'rewritten',
            }]), 'second',
        )

    def test_moderator_revert_writes_audit_row(self):
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:article-revert', args=[self.place.slug]),
            {'revision_id': self.old.id, 'comment': ''},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        entry = ModAction.objects.get(action=ModAction.Action.REVERT_ARTICLE)
        self.assertEqual(entry.actor, self.mod)
        self.assertEqual(entry.target_user, self.author)

    def test_ordinary_revert_writes_no_audit_row(self):
        """Revert is a normal content tool for regular users — only a
        moderator's use of it is a moderation act worth logging."""
        self.client.force_login(self.author)
        self.client.post(
            reverse('core:article-revert', args=[self.place.slug]),
            {'revision_id': self.old.id, 'comment': ''},
            content_type='application/json',
        )
        self.assertFalse(
            ModAction.objects.filter(
                action=ModAction.Action.REVERT_ARTICLE
            ).exists()
        )


class AuditFeedTests(ApiTestCase):
    """The global chronological feed."""

    def setUp(self):
        super().setUp()
        self.mod = User.objects.create_user('mod', password='x', is_staff=True)
        self.other = User.objects.create_user('other', password='x')
        ModAction.objects.create(
            actor=self.mod, action=ModAction.Action.BAN_USER,
            target_user=self.other, reason='spam',
        )
        ModAction.objects.create(
            actor=self.mod, action=ModAction.Action.DELETE_POST,
            target_user=self.other,
        )

    def test_feed_lists_actions_newest_first(self):
        self.client.force_login(self.mod)
        rows = self.client.get(reverse('core:mod-audit')).json()['actions']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['action'], ModAction.Action.DELETE_POST)
        self.assertEqual(rows[0]['actor'], 'mod')
        self.assertEqual(rows[0]['target_user'], 'other')

    def test_feed_filters_by_action(self):
        self.client.force_login(self.mod)
        rows = self.client.get(
            reverse('core:mod-audit'), {'action': 'ban_user'}
        ).json()['actions']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reason'], 'spam')

    def test_feed_filters_by_actor(self):
        self.client.force_login(self.mod)
        rows = self.client.get(
            reverse('core:mod-audit'), {'actor': self.other.id}
        ).json()['actions']
        self.assertEqual(rows, [])

    def test_feed_requires_moderator(self):
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(reverse('core:mod-audit')).status_code, 403
        )

    def test_feed_rejects_non_numeric_ids(self):
        # A non-numeric id reached the queryset and raised ValueError, so a
        # hand-typed URL 500'd. Note '1 OR 1=1' is not an injection risk —
        # the ORM parameterizes — it just took the same crashing path.
        self.client.force_login(self.mod)
        for params in (
            {'actor': 'abc'},
            {'target': 'abc'},
            {'actor': '1 OR 1=1'},
        ):
            with self.subTest(params=params):
                response = self.client.get(
                    reverse('core:mod-audit'), params
                )
                self.assertEqual(response.status_code, 400)

    def test_feed_rejects_unknown_action(self):
        self.client.force_login(self.mod)
        response = self.client.get(
            reverse('core:mod-audit'), {'action': 'wizardry'}
        )
        self.assertEqual(response.status_code, 400)

    def test_feed_ignores_blank_filters(self):
        # '?actor=' has always meant "no filter" and must keep meaning it.
        self.client.force_login(self.mod)
        rows = self.client.get(
            reverse('core:mod-audit'),
            {'actor': '', 'target': '', 'action': ''},
        ).json()['actions']
        self.assertEqual(len(rows), 2)


class SlugAliasTests(ApiTestCase):
    """The slug alias table: creation invariant, alias lookups, the /place
    301, and the rename_place command (docs/slug-renames.md)."""

    def setUp(self):
        super().setUp()
        self.place = _make_place(name='Ojai', slug='ojai-california')

    def _rename(self, *args):
        call_command('rename_place', *args, stdout=StringIO())

    def test_creation_mints_one_canonical_slug(self):
        rows = PlaceSlug.objects.filter(place=self.place)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.slug, 'ojai-california')
        self.assertTrue(row.is_canonical)

    def test_unique_slug_skips_aliases(self):
        # Park 'ojai-2' as an alias of an unrelated place, then a fresh place
        # named "Ojai" must step over it to 'ojai-3' (not just past Place.slug).
        other = _make_place(name='Ojai Elsewhere', slug='ojai')
        PlaceSlug.objects.create(place=other, slug='ojai-2', is_canonical=False)
        from .slugs import unique_slug
        self.assertEqual(unique_slug('Ojai'), 'ojai-3')

    def test_api_resolves_alias_to_canonical_payload(self):
        PlaceSlug.objects.create(
            place=self.place, slug='ojai-2', is_canonical=False
        )
        response = self.client.get(
            reverse('core:place-detail', args=['ojai-2'])
        )
        self.assertEqual(response.status_code, 200)
        # The payload reports the canonical slug, so the SPA can heal its URL.
        self.assertEqual(response.json()['place']['slug'], 'ojai-california')

    def test_api_unknown_slug_404s(self):
        response = self.client.get(
            reverse('core:place-detail', args=['no-such-place'])
        )
        self.assertEqual(response.status_code, 404)

    def test_place_page_301s_alias_to_canonical(self):
        PlaceSlug.objects.create(
            place=self.place, slug='ojai-2', is_canonical=False
        )
        response = self.client.get('/place/ojai-2')
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], '/place/ojai-california')

    def test_rename_to_free_slug_keeps_old_as_alias(self):
        self._rename('ojai-california', 'ojai')
        self.place.refresh_from_db()
        self.assertEqual(self.place.slug, 'ojai')
        slugs = {
            (s.slug, s.is_canonical)
            for s in PlaceSlug.objects.filter(place=self.place)
        }
        self.assertEqual(
            slugs, {('ojai', True), ('ojai-california', False)}
        )
        # Old URL still resolves via 301; new one is canonical.
        self.assertEqual(
            self.client.get('/place/ojai-california').status_code, 301
        )
        self.assertEqual(
            self.client.get(
                reverse('core:place-detail', args=['ojai'])
            ).json()['place']['slug'],
            'ojai',
        )

    def test_rename_can_target_place_by_its_alias(self):
        PlaceSlug.objects.create(
            place=self.place, slug='ojai-2', is_canonical=False
        )
        self._rename('ojai-2', 'ojai')
        self.place.refresh_from_db()
        self.assertEqual(self.place.slug, 'ojai')

    def test_rename_to_slug_held_by_another_place_refused(self):
        other = _make_place(name='Ojai Restaurant', slug='ojai')
        with self.assertRaises(CommandError):
            self._rename('ojai-california', 'ojai')
        # Nothing moved on either side.
        self.place.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.place.slug, 'ojai-california')
        self.assertEqual(other.slug, 'ojai')

    def test_rename_to_own_alias_promotes_it(self):
        PlaceSlug.objects.create(
            place=self.place, slug='ojai-2', is_canonical=False
        )
        self._rename('ojai-california', 'ojai-2')
        self.place.refresh_from_db()
        self.assertEqual(self.place.slug, 'ojai-2')
        # No new row was minted; the alias was flipped, old canonical demoted.
        self.assertEqual(PlaceSlug.objects.filter(place=self.place).count(), 2)
        canonical = PlaceSlug.objects.get(place=self.place, is_canonical=True)
        self.assertEqual(canonical.slug, 'ojai-2')

    def test_rename_to_invalid_slug_refused(self):
        with self.assertRaises(CommandError):
            self._rename('ojai-california', 'Not A Slug')
        self.place.refresh_from_db()
        self.assertEqual(self.place.slug, 'ojai-california')

    def test_rename_unknown_place_refused(self):
        with self.assertRaises(CommandError):
            self._rename('no-such-place', 'whatever')

    def test_rename_dry_run_writes_nothing(self):
        self._rename('ojai-california', 'ojai', '--dry-run')
        self.place.refresh_from_db()
        self.assertEqual(self.place.slug, 'ojai-california')
        self.assertFalse(
            PlaceSlug.objects.filter(slug='ojai').exists()
        )

    def test_sitemap_emits_only_canonical(self):
        author = User.objects.create_user('ojai-author', password='pw12345!')
        save_edit(self.place, author, _content(), 'seed')
        PlaceSlug.objects.create(
            place=self.place, slug='ojai-2', is_canonical=False
        )
        xml = _sitemap_xml(self.client.get('/sitemap.xml'))
        self.assertIn('/place/ojai-california', xml)
        self.assertNotIn('/place/ojai-2', xml)


class ReadEndpointBoundsTests(ApiTestCase):
    """The public read paths are capped so one heavily-edited article or one
    runaway thread can't build an unbounded response in memory. History
    paginates (old revisions must stay reachable); talk truncates."""

    def setUp(self):
        super().setUp()
        self.place = _make_place()
        self.user = User.objects.create_user('drew', password='pw12345!')

    def _revisions(self, count):
        for i in range(count):
            save_edit(self.place, self.user, _content(), f'edit {i}')

    def test_history_page_is_capped_and_reports_more(self):
        self._revisions(views.MAX_REVISIONS_PER_PAGE + 5)
        url = reverse('core:revision-list', args=[self.place.slug])
        body = self.client.get(url).json()
        self.assertEqual(
            len(body['revisions']), views.MAX_REVISIONS_PER_PAGE
        )
        self.assertEqual(body['total'], views.MAX_REVISIONS_PER_PAGE + 5)
        self.assertTrue(body['has_more'])

    def test_history_offset_reaches_the_oldest_revisions(self):
        total = views.MAX_REVISIONS_PER_PAGE + 5
        self._revisions(total)
        url = reverse('core:revision-list', args=[self.place.slug])
        first = self.client.get(url).json()
        rest = self.client.get(
            url, {'offset': views.MAX_REVISIONS_PER_PAGE}
        ).json()
        self.assertEqual(len(rest['revisions']), 5)
        self.assertFalse(rest['has_more'])
        # No overlap and nothing skipped: the two pages are the whole history.
        ids = [r['id'] for r in first['revisions'] + rest['revisions']]
        self.assertEqual(len(set(ids)), total)

    def test_history_junk_offset_falls_back_to_first_page(self):
        self._revisions(3)
        url = reverse('core:revision-list', args=[self.place.slug])
        for bad in ['abc', '-5', '']:
            body = self.client.get(url, {'offset': bad}).json()
            self.assertEqual(body['offset'], 0)
            self.assertEqual(len(body['revisions']), 3)

    def test_history_offset_past_the_end_is_empty_not_an_error(self):
        self._revisions(3)
        url = reverse('core:revision-list', args=[self.place.slug])
        response = self.client.get(url, {'offset': 999})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['revisions'], [])
        self.assertFalse(body['has_more'])

    def test_stub_history_has_the_same_shape_as_a_real_page(self):
        url = reverse('core:revision-list', args=[self.place.slug])
        body = self.client.get(url).json()
        self.assertEqual(
            body,
            {'revisions': [], 'total': 0, 'offset': 0, 'has_more': False},
        )

    def test_talk_thread_list_is_capped(self):
        for i in range(views.MAX_TALK_THREADS + 3):
            TalkThread.objects.create(place=self.place, title=f't{i}')
        body = self.client.get(
            reverse('core:talk', args=[self.place.slug])
        ).json()
        self.assertEqual(len(body['threads']), views.MAX_TALK_THREADS)
        self.assertTrue(body['has_more'])

    def test_long_thread_is_truncated_and_says_so(self):
        thread = TalkThread.objects.create(place=self.place, title='t')
        for i in range(views.MAX_TALK_POSTS + 2):
            TalkPost.objects.create(
                thread=thread, author=self.user, body_md=f'post {i}'
            )
        body = self.client.get(
            reverse('core:talk', args=[self.place.slug])
        ).json()
        row = body['threads'][0]
        self.assertEqual(len(row['posts']), views.MAX_TALK_POSTS)
        self.assertTrue(row['posts_truncated'])

    def test_talk_post_fetch_is_bounded_in_sql_not_python(self):
        """The per-thread cap has to be enforced by the query, or a huge
        thread still lands in memory before being sliced. Django compiles the
        sliced Prefetch into a ROW_NUMBER() window, so the posts query returns
        at most the cap per thread — check the SQL, since a Python-side slice
        would pass every other assertion in this class."""
        thread = TalkThread.objects.create(place=self.place, title='t')
        for i in range(views.MAX_TALK_POSTS + 2):
            TalkPost.objects.create(
                thread=thread, author=self.user, body_md=f'post {i}'
            )
        with CaptureQueriesContext(connection) as captured:
            self.client.get(reverse('core:talk', args=[self.place.slug]))
        post_queries = [
            q['sql'] for q in captured.captured_queries
            if 'core_talkpost' in q['sql']
        ]
        self.assertEqual(len(post_queries), 1)
        self.assertIn('ROW_NUMBER', post_queries[0].upper())

    def test_short_thread_is_not_marked_truncated(self):
        thread = TalkThread.objects.create(place=self.place, title='t')
        TalkPost.objects.create(
            thread=thread, author=self.user, body_md='only post'
        )
        body = self.client.get(
            reverse('core:talk', args=[self.place.slug])
        ).json()
        row = body['threads'][0]
        self.assertEqual(len(row['posts']), 1)
        self.assertFalse(row['posts_truncated'])

    def test_sitemap_streams(self):
        save_edit(self.place, self.user, _content(), 'seed')
        response = self.client.get('/sitemap.xml')
        self.assertTrue(response.streaming)
        xml = _sitemap_xml(response)
        self.assertTrue(xml.startswith('<?xml'))
        self.assertTrue(xml.endswith('</urlset>'))
        self.assertIn(f'/place/{self.place.slug}', xml)


class ResolvePermissionTests(ApiTestCase):
    """Who may spend an Overpass query and create a Place.

    Anonymous callers get the database half of the ladder: a place we
    already know opens normally, but a first-ever lookup — the part that
    calls Overpass under our own IP and writes a permanent row — needs an
    account. Banned users get neither.
    """

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('drew', password='pw12345!')
        self.mod = User.objects.create_user('mod', password='pw12345!')

    def _post(self, **overrides):
        payload = {
            'name': 'Mississippi River',
            'class': 'waterway',
            'lngLat': [-91.0, 32.0],
            'zoom': 8,
        }
        payload.update(overrides)
        return self.client.post(
            reverse('core:resolve'), payload, content_type='application/json'
        )

    @patch('core.resolve.overpass.fetch_elements')
    def test_anonymous_cannot_create_and_never_calls_overpass(self, fetch):
        fetch.return_value = [_relation()]
        response = self._post()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'signin_required')
        # The point of the restriction: no outbound traffic, no new row.
        fetch.assert_not_called()
        self.assertEqual(Place.objects.count(), 0)

    @patch('core.resolve.overpass.fetch_elements')
    def test_anonymous_can_open_an_already_known_place(self, fetch):
        """The half that stays public — otherwise every logged-out click on
        an existing article would hit a sign-in wall."""
        fetch.return_value = [_relation()]
        self.client.force_login(self.user)
        created = self._post()
        self.assertEqual(created.status_code, 200)
        self.assertTrue(created.json()['created'])
        slug = created.json()['place']['slug']

        self.client.logout()
        fetch.reset_mock()
        again = self._post()
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()['created'])
        self.assertEqual(again.json()['place']['slug'], slug)
        # Cache hit: still no Overpass call, and still one row.
        fetch.assert_not_called()
        self.assertEqual(Place.objects.count(), 1)

    @patch('core.resolve.overpass.fetch_elements')
    def test_banned_user_cannot_resolve(self, fetch):
        fetch.return_value = [_relation()]
        Ban.objects.create(user=self.user, created_by=self.mod)
        self.client.force_login(self.user)
        response = self._post()
        self.assertEqual(response.status_code, 403)
        fetch.assert_not_called()
        self.assertEqual(Place.objects.count(), 0)

    @patch('core.resolve.overpass.fetch_elements')
    def test_lifted_ban_restores_resolution(self, fetch):
        fetch.return_value = [_relation()]
        ban = Ban.objects.create(user=self.user, created_by=self.mod)
        ban.lifted = timezone.now()
        ban.save(update_fields=['lifted'])
        self.client.force_login(self.user)
        self.assertEqual(self._post().status_code, 200)

    def test_anonymous_bad_payload_still_400s(self):
        """Validation order matters: a malformed body is a client error
        whether or not you're signed in, and shouldn't read as a login wall."""
        self.assertEqual(self._post(lngLat=[-91.0]).status_code, 400)


@override_settings(ALLOWED_HOSTS=['testserver'])
class ContentSecurityPolicyTests(TestCase):
    """The second layer behind react-markdown's no-raw-HTML configuration.

    These pin the parts that fail *silently* if they regress: a blocked
    inline script and a missing origin both look like unrelated bugs.
    """

    def _csp(self, response):
        return {
            part.split(' ')[0]: part
            for part in response.headers['Content-Security-Policy'].split('; ')
        }

    def test_shell_stamps_its_inline_script_with_the_header_nonce(self):
        """The whole point of the nonce plumbing. If these two ever disagree,
        the dark-mode anti-flash snippet is dropped by the browser and
        returning readers get a white flash with nothing in the logs."""
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        header_nonce = re.search(
            r"'nonce-([\w-]+)'", self._csp(response)['script-src']
        )
        self.assertIsNotNone(header_nonce)
        html = response.content.decode()
        self.assertIn(f'<script nonce="{header_nonce.group(1)}">', html)

    def test_bundle_script_tag_is_left_alone(self):
        """Only inline scripts need stamping; Vite's tags carry src=."""
        html = self.client.get('/').content.decode()
        tags = re.findall(r'<script[^>]*>', html)
        src_tags = [t for t in tags if ' src=' in t]
        self.assertTrue(src_tags)
        for tag in src_tags:
            self.assertNotIn('nonce=', tag)

    def test_nonce_is_per_request(self):
        first = self._csp(self.client.get('/'))['script-src']
        second = self._csp(self.client.get('/'))['script-src']
        self.assertNotEqual(first, second)

    def test_no_nonce_is_minted_for_responses_without_inline_scripts(self):
        """LazyNonce only generates on access, so an API response shouldn't
        carry one. Guards the laziness, not just the header."""
        response = self.client.get('/api/highlights/?bbox=0,0,1,1')
        self.assertEqual(self._csp(response)['script-src'], "script-src 'self'")

    def test_map_and_geocoder_origins_are_allowed(self):
        """MapLibre and Photon are the only external origins. A too-strict
        policy here breaks the map in ways that read as unrelated bugs."""
        csp = self._csp(self.client.get('/'))
        self.assertIn('https://tiles.openfreemap.org', csp['connect-src'])
        self.assertIn('https://photon.komoot.io', csp['connect-src'])
        self.assertIn('https://tiles.openfreemap.org', csp['img-src'])
        # MapLibre runs its tile decoders in workers created from blob: URLs.
        self.assertIn('blob:', csp['worker-src'])

    def test_the_locked_down_directives(self):
        csp = self._csp(self.client.get('/'))
        self.assertEqual(csp['object-src'], "object-src 'none'")
        self.assertEqual(csp['frame-ancestors'], "frame-ancestors 'none'")
        self.assertEqual(csp['base-uri'], "base-uri 'self'")
        self.assertEqual(csp['form-action'], "form-action 'self'")
        self.assertNotIn('unsafe-inline', csp['script-src'])
        self.assertNotIn('unsafe-eval', csp['script-src'])


class TermsAcceptanceTests(TestCase):
    """The Terms-of-Use gate on signup.

    TERMS.md §2 has every contributor license their edits CC BY-SA 4.0, and
    that grant only binds someone who actually agreed — so agreement is a
    server-side precondition of getting an account, and each one is recorded
    against the version of the document the user was shown.
    """

    SIGNUP = '/_allauth/browser/v1/auth/signup'

    def _signup(self, **extra):
        body = {
            'username': 'newuser',
            'email': 'newuser@example.com',
            'password': 'sturdy-passphrase-9',
        }
        body.update(extra)
        return self.client.post(
            self.SIGNUP, body, content_type='application/json'
        )

    def test_signup_without_agreement_is_rejected(self):
        # The point of validating server-side: posting straight to the
        # endpoint, past the React checkbox, still cannot create an account.
        response = self._signup()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username='newuser').exists())

    def test_signup_refusing_agreement_is_rejected(self):
        response = self._signup(terms=False)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username='newuser').exists())

    def test_agreement_is_recorded_with_version(self):
        response = self._signup(terms=True)
        # 401 = created, pending email verification (mandatory).
        self.assertEqual(response.status_code, 401)
        user = User.objects.get(username='newuser')
        acceptance = TermsAcceptance.objects.get(user=user)
        self.assertEqual(acceptance.version, TERMS_VERSION)
        self.assertIsNotNone(acceptance.accepted)

    def test_version_matches_the_document(self):
        """The recorded version has to name a real revision of TERMS.md, or
        the record proves nothing. Guards against updating the document and
        forgetting the constant (or the reverse)."""
        documented = documented_version()
        if documented is None:
            self.skipTest('TERMS.md not present next to the server')
        self.assertEqual(
            documented,
            TERMS_VERSION,
            'TERMS.md "Last updated" and core.terms.TERMS_VERSION disagree — '
            'update both when the Terms change.',
        )


class TermsPageTests(TestCase):
    """/terms is a real URL, not only an in-app dialog: the DMCA safe harbor
    needs the designated agent's contact publicly accessible."""

    def test_terms_url_serves_the_app(self):
        response = self.client.get('/terms')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Terms of Use', response.content.decode())

    def test_terms_is_listed_in_the_sitemap(self):
        response = self.client.get(reverse('sitemap'))
        body = b''.join(response.streaming_content).decode()
        self.assertIn('/terms', body)
