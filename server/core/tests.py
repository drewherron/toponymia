import json
import logging
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import requests
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.gis.geos import (
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
)
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from . import dashboard, notify, overpass, views
from .articles import save_edit
from .csp import MAX_REPORTS, REPORT_ROUTE
from .feature_classes import ALLOWED_FEATURE_CLASSES
from .models import (
    AdminArea,
    Article,
    Ban,
    BannedEmail,
    ModAction,
    Place,
    PlaceName,
    PlaceSlug,
    Report,
    ReservedUsername,
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
    MAX_ELEMENTS,
    MAX_ETYMOLOGIES,
    MAX_MARKDOWN,
    MAX_NAMES,
    MAX_REFERENCES,
)
from .terms import TERMS_VERSION, documented_version

_no_http = None


def setUpModule():
    """Fail loudly on any Overpass call a test forgot to mock.

    Without this the suite reaches the real network: adding
    `fetch_place_nodes` to the resolve path silently turned 20 tests from
    10 s into 193 s of live HTTP and retries, which would be flaky in CI
    and dishonest everywhere. A test that genuinely wants the HTTP layer
    patches `requests.post` itself, and its patch wins over this one.
    """
    global _no_http

    def refuse(*args, **kwargs):
        raise AssertionError(
            'unmocked Overpass HTTP in tests — patch the fetch_* function '
            'you are exercising (see ApiTestCase for the default stubs)'
        )

    _no_http = patch('core.overpass.requests.post', side_effect=refuse)
    _no_http.start()


def tearDownModule():
    if _no_http is not None:
        _no_http.stop()


class ApiTestCase(TestCase):
    """Base for API tests: clears the throttle cache before each test so
    DRF's per-endpoint rate limits (shared LocMemCache) don't bleed across
    the suite while still being exercised within a test."""

    def setUp(self):
        super().setUp()
        cache.clear()
        # The locality rung makes up to two *extra* Overpass calls beyond
        # the one that finds the feature: the containing-area intersection
        # for anything longer than PROBE_MIN_EXTENT_M, and the settlement
        # node lookup when containment found nothing better than a
        # district. Most tests care about neither and none should reach the
        # network to find out, so both are stubbed empty here; a test
        # exercising either patches it with its own data.
        #
        # Both were reaching the real network before setUpModule's guard
        # existed — the failures were swallowed by the `except
        # OverpassError` that makes these calls optional, so the tests
        # passed while quietly doing live HTTP.
        for target in ('fetch_common_areas', 'fetch_place_nodes'):
            stub = patch(f'core.resolve.overpass.{target}', return_value=[])
            stub.start()
            self.addCleanup(stub.stop)


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

    def test_settlement_click_beats_the_province_of_the_same_name(self):
        """Havana, as OSM really tags it (checked 2026-08-16).

        Relation 1854615 is *Havana Province* (Q12588) and carries
        `place=city` besides, because it is tagged for the capital inside
        it. Node 26396457 is the city (Q1563). On the type rank alone the
        relation won, so clicking the city's own label opened an article
        about the province.
        """
        city = {
            'type': 'node', 'id': 26396457, 'lat': 23.13, 'lon': -82.38,
            'tags': {
                'name': 'La Habana', 'wikidata': 'Q1563',
                'place': 'city', 'capital': 'yes',
            },
        }
        province = _relation(
            osm_id=1854615, name='La Habana', qid='Q12588',
        )
        province['tags'].update({
            'place': 'city',
            'boundary': 'administrative',
            'admin_level': '4',
        })
        self.assertEqual(choose_element([province, city], 'city'), city)
        self.assertEqual(choose_element([city, province], 'city'), city)

    def test_admin_click_still_gets_the_admin_area(self):
        """The other direction of the same Havana data: a click on the
        province's own label must not be dragged onto the city."""
        city = {
            'type': 'node', 'id': 26396457, 'lat': 23.13, 'lon': -82.38,
            'tags': {
                'name': 'La Habana', 'wikidata': 'Q1563', 'place': 'city',
            },
        }
        province = _relation(
            osm_id=1854615, name='La Habana', qid='Q12588',
        )
        province['tags'].update({
            'place': 'city',
            'boundary': 'administrative',
            'admin_level': '4',
        })
        self.assertEqual(
            choose_element([city, province], 'state'), province
        )

    def test_settlement_click_beats_the_country_of_the_same_name(self):
        """Panama City (node 1242998122, Q3306) used to resolve to
        relation 287668 — the country of Panama, Q804."""
        city = {
            'type': 'node', 'id': 1242998122, 'lat': 8.98, 'lon': -79.52,
            'tags': {
                'name': 'Panamá', 'wikidata': 'Q3306', 'place': 'city',
            },
        }
        country = _relation(osm_id=287668, name='Panamá', qid='Q804')
        country['tags'].update({
            'boundary': 'administrative', 'admin_level': '2',
        })
        self.assertEqual(choose_element([country, city], 'city'), city)

    def test_citys_own_boundary_relation_still_wins(self):
        """Paris: node 17807753 and relation 7444 are both Q90, so they
        are one entity and the relation is still the better anchor. The
        entity rung must not cost us the geometry in the common case."""
        node = {
            'type': 'node', 'id': 17807753, 'lat': 48.86, 'lon': 2.35,
            'tags': {'name': 'Paris', 'wikidata': 'Q90', 'place': 'city'},
        }
        boundary = _relation(osm_id=7444, name='Paris', qid='Q90')
        boundary['tags'].update({
            'boundary': 'administrative', 'admin_level': '8',
        })
        self.assertEqual(
            choose_element([node, boundary], 'city'), boundary
        )

    def test_non_settlement_click_is_untouched(self):
        """A river clicked beside a same-named town keeps resolving to the
        river: only a settlement click seeds the entity rung."""
        town = {
            'type': 'node', 'id': 5, 'lat': 45.0, 'lon': -122.0,
            'tags': {
                'name': 'Mill Creek', 'wikidata': 'Q9', 'place': 'town',
            },
        }
        river = _relation(osm_id=1236, name='Mill Creek', qid='Q1497')
        self.assertEqual(
            choose_element([town, river], 'waterway'), river
        )

    def test_settlement_without_a_qid_does_not_seed(self):
        """No wikidata tag on the settlement means no evidence of which
        entity was clicked, so the ladder is left as it was."""
        town = {
            'type': 'node', 'id': 5, 'lat': 45.0, 'lon': -122.0,
            'tags': {'name': 'Springfield', 'place': 'town'},
        }
        county = _relation(osm_id=99, name='Springfield', qid='Q7')
        county['tags']['boundary'] = 'administrative'
        self.assertEqual(choose_element([town, county], 'town'), county)

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
    def __init__(self, elements=None, http_error=None, status_code=200):
        self._elements = elements or []
        self._http_error = http_error
        # Read before raise_for_status: 429 is handled by status rather
        # than by exception, so that it keeps its own retry policy.
        self.status_code = status_code

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


class OverpassRateLimitTests(TestCase):
    """429 is not 504, and must not be retried like one."""

    def _limited(self):
        return _FakeResponse(status_code=429)

    @patch('core.overpass.time.sleep')
    @patch('core.overpass.requests.post')
    def test_a_rate_limit_is_retried_once_not_four_times(self, post, sleep):
        """Asking again a second later is what deepens a rate limit.

        The transient ladder makes four passes; this must not.
        """
        from core.overpass import OVERPASS_URLS, RATE_LIMIT_RETRIES
        post.return_value = self._limited()
        with self.assertRaises(OverpassError):
            fetch_elements('Nowhere', 0.0, 0.0, 500)
        self.assertEqual(
            post.call_count,
            (1 + RATE_LIMIT_RETRIES) * len(OVERPASS_URLS),
        )

    @patch('core.overpass.time.sleep')
    @patch('core.overpass.requests.post')
    def test_a_rate_limit_backs_off_further_than_a_transient(self, post,
                                                             sleep):
        from core.overpass import RATE_LIMIT_BACKOFF_S, RETRY_BACKOFFS_S
        post.return_value = self._limited()
        with self.assertRaises(OverpassError):
            fetch_elements('Nowhere', 0.0, 0.0, 500)
        waited = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(waited, [RATE_LIMIT_BACKOFF_S])
        self.assertGreater(RATE_LIMIT_BACKOFF_S, RETRY_BACKOFFS_S[0])

    @patch('core.overpass.time.sleep')
    @patch('core.overpass.requests.post')
    def test_a_rate_limit_is_logged_as_a_warning(self, post, sleep):
        """The one Overpass signal that must never be silent again.

        Rate limiting is what named a Mexborough street after Doncaster and
        a Fântânele street after its commune, and it was invisible at the
        time because nothing recorded a request (LESSONS.md, 'a silent
        fallback also hides the thing causing it'). It is also the number
        that decides whether a seeding pace is too fast, so it is asserted
        at WARNING rather than left to INFO with the successes.
        """
        post.return_value = self._limited()
        with self.assertLogs('core.overpass', level='WARNING') as logs:
            with self.assertRaises(OverpassError):
                fetch_elements('Nowhere', 0.0, 0.0, 500)
        self.assertTrue(
            any('429' in line for line in logs.output),
            f'no 429 in {logs.output}',
        )

    @patch('core.overpass.time.sleep')
    @patch('core.overpass.requests.post')
    def test_a_successful_call_is_logged_with_its_elapsed_time(self, post,
                                                               sleep):
        """Latency per call is the other half of the watch list's metric.

        Without it a slow Overpass and a fast one look identical in the
        log, and 'Overpass latency from /api/resolve' has nothing to read.
        """
        post.return_value = _FakeResponse(elements=[_relation()])
        with self.assertLogs('core.overpass', level='INFO') as logs:
            fetch_elements('X', 0.0, 0.0, 500)
        self.assertTrue(
            any('200 in' in line and 'ms' in line for line in logs.output),
            f'no timed success in {logs.output}',
        )

    @patch('core.overpass.time.sleep')
    @patch('core.overpass.requests.post')
    def test_a_rate_limit_on_one_mirror_still_tries_the_others(self, post,
                                                              sleep):
        from core.overpass import OVERPASS_URLS
        if len(OVERPASS_URLS) < 2:
            self.skipTest('single mirror configured')
        post.side_effect = [self._limited(),
                            _FakeResponse(elements=[_relation()])]
        self.assertEqual(len(fetch_elements('X', 0.0, 0.0, 500)), 1)

    @patch('core.overpass.time.sleep')
    @patch('core.overpass.requests.post')
    def test_a_504_still_uses_the_transient_ladder(self, post, sleep):
        # The distinction is the whole point: "no free slot" clears in
        # seconds and is worth riding out.
        from core.overpass import OVERPASS_URLS, RETRY_BACKOFFS_S
        post.side_effect = requests.exceptions.ConnectionError('no free slot')
        with self.assertRaises(OverpassError):
            fetch_elements('Nowhere', 0.0, 0.0, 500)
        self.assertEqual(
            post.call_count,
            (1 + len(RETRY_BACKOFFS_S)) * len(OVERPASS_URLS),
        )


class OverpassBudgetTests(TestCase):
    """The request-wide deadline — what keeps the retry ladders from
    outlasting gunicorn's --timeout and killing the worker."""

    def setUp(self):
        self.now = 1000.0

    def _clock(self):
        return self.now

    @patch('core.overpass.time.sleep')
    @patch('core.overpass.requests.post')
    def test_without_a_budget_nothing_changes(self, post, sleep):
        from core.overpass import OVERPASS_URLS, RETRY_BACKOFFS_S
        post.side_effect = requests.exceptions.ConnectionError('down')
        with self.assertRaises(OverpassError):
            fetch_elements('Nowhere', 0.0, 0.0, 500)
        self.assertEqual(
            post.call_count,
            (1 + len(RETRY_BACKOFFS_S)) * len(OVERPASS_URLS),
        )

    @patch('core.overpass.requests.post')
    def test_an_exhausted_budget_stops_retrying(self, post):
        """The failure that used to be a killed worker and a 502."""
        post.side_effect = requests.exceptions.ConnectionError('down')

        def sleeper(seconds):
            self.now += seconds

        with patch('core.overpass.time.monotonic', self._clock), \
                patch('core.overpass.time.sleep', sleeper):
            with overpass.budget(2):
                with self.assertRaises(OverpassError):
                    fetch_elements('Nowhere', 0.0, 0.0, 500)

        # First pass, then a 1 s backoff fits; the 3 s one does not.
        from core.overpass import OVERPASS_URLS
        self.assertEqual(post.call_count, 2 * len(OVERPASS_URLS))

    @patch('core.overpass.requests.post')
    def test_no_attempt_is_started_that_cannot_finish(self, post):
        post.side_effect = requests.exceptions.ConnectionError('down')
        with patch('core.overpass.time.monotonic', self._clock), \
                patch('core.overpass.time.sleep'):
            with overpass.budget(0):
                with self.assertRaises(OverpassError):
                    fetch_elements('Nowhere', 0.0, 0.0, 500)
        post.assert_not_called()

    @patch('core.overpass.requests.post')
    def test_the_socket_timeout_is_clamped_to_what_is_left(self, post):
        """A 15 s socket wait inside a 5 s budget would overshoot."""
        post.return_value = _FakeResponse(elements=[_relation()])
        with patch('core.overpass.time.monotonic', self._clock):
            with overpass.budget(5):
                fetch_elements('X', 0.0, 0.0, 500)
        self.assertEqual(post.call_args.kwargs['timeout'], 5)

    @patch('core.overpass.requests.post')
    def test_a_generous_budget_leaves_the_timeout_alone(self, post):
        from core.overpass import TIMEOUT_S
        post.return_value = _FakeResponse(elements=[_relation()])
        with patch('core.overpass.time.monotonic', self._clock):
            with overpass.budget(600):
                fetch_elements('X', 0.0, 0.0, 500)
        self.assertEqual(post.call_args.kwargs['timeout'], TIMEOUT_S)

    def test_the_budget_is_released_afterwards(self):
        import math
        with overpass.budget(5):
            self.assertLess(overpass._seconds_left(), 6)
        self.assertEqual(overpass._seconds_left(), math.inf)


class OverpassServerTimeoutTests(TestCase):
    """Overpass must give up before we do, or it holds a slot we abandoned.

    Only two queries per IP may be in flight at once, so a query we walk away
    from is half our capacity spent on an answer nobody will read — which is
    how a strictly serial crawler earns a 429 (2026-08-26).
    """

    def setUp(self):
        self.now = 1000.0

    def _clock(self):
        return self.now

    def _server_timeout(self, post):
        query = post.call_args.kwargs['data']['data']
        return int(re.search(r'\[timeout:(\d+)\]', query).group(1))

    @patch('core.overpass.requests.post')
    def test_the_server_gives_up_before_the_socket_does(self, post):
        from core.overpass import TIMEOUT_S
        post.return_value = _FakeResponse(elements=[_relation()])
        with patch('core.overpass.time.monotonic', self._clock):
            with overpass.budget(600):
                fetch_elements('X', 0.0, 0.0, 500)
        self.assertLess(self._server_timeout(post),
                        post.call_args.kwargs['timeout'])
        # fetch_elements asks for 10, which is already under the 13 a 15 s
        # socket allows, so its own judgement stands.
        self.assertEqual(self._server_timeout(post), 10)
        self.assertEqual(post.call_args.kwargs['timeout'], TIMEOUT_S)

    @patch('core.overpass.requests.post')
    def test_a_squeezed_budget_squeezes_the_server_timeout_too(self, post):
        """The case a hardcoded [timeout:25] could never satisfy.

        `_seconds_left()` drove the socket wait down to 4.1 s in the crawl
        log while the query still asked Overpass for 25 s of work.
        """
        post.return_value = _FakeResponse(elements=[_relation()])
        with patch('core.overpass.time.monotonic', self._clock):
            with overpass.budget(6):
                fetch_elements('X', 0.0, 0.0, 500)
        self.assertEqual(post.call_args.kwargs['timeout'], 6)
        self.assertEqual(self._server_timeout(post), 4)

    @patch('core.overpass.requests.post')
    def test_the_server_timeout_never_falls_below_one_second(self, post):
        post.return_value = _FakeResponse(elements=[_relation()])
        with patch('core.overpass.time.monotonic', self._clock):
            with overpass.budget(1.5):
                fetch_elements('X', 0.0, 0.0, 500)
        self.assertEqual(self._server_timeout(post), 1)

    def test_a_generous_socket_does_not_relax_a_tight_query(self):
        """min(), not overwrite — a query's own ceiling is a judgement."""
        from core.overpass import _with_server_timeout
        self.assertIn('[timeout:10]',
                      _with_server_timeout('[out:json][timeout:10];x', 60))

    def test_a_tight_socket_clamps_a_generous_query(self):
        from core.overpass import _with_server_timeout
        self.assertIn('[timeout:13]',
                      _with_server_timeout('[out:json][timeout:25];x', 15))

    def test_only_the_leading_directive_is_rewritten(self):
        """A literal 'timeout:' inside the query body is not ours to touch."""
        from core.overpass import _with_server_timeout
        out = _with_server_timeout(
            '[out:json][timeout:25];node["name"="timeout:99"];out;', 15)
        self.assertIn('[timeout:13]', out)
        self.assertIn('"timeout:99"', out)

    @patch('core.overpass.requests.post')
    def test_the_invariant_holds_for_the_long_relation_query(self, post):
        """The relation fetch asks for its own, much longer socket wait."""
        post.return_value = _FakeResponse(elements=[])
        with patch('core.overpass.time.monotonic', self._clock):
            overpass.fetch_relation_member_ways(1)
        self.assertLess(self._server_timeout(post),
                        post.call_args.kwargs['timeout'])


class ResolveBudgetTests(ApiTestCase):
    """The web path spends a bounded amount on Overpass; batch callers do
    not, because nothing is killing them."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('budget', password='pw12345!')
        self.client.force_login(self.user)

    @patch('core.resolve.overpass.fetch_elements')
    def test_the_view_sets_a_budget(self, fetch):
        import math
        seen = {}

        def record(*args, **kwargs):
            seen['left'] = overpass._seconds_left()
            return []

        fetch.side_effect = record
        self.client.post(
            reverse('core:resolve'),
            {'name': 'Ojai', 'class': 'city', 'lngLat': [-119.2, 34.4],
             'zoom': 12},
            content_type='application/json',
        )
        self.assertNotEqual(seen['left'], math.inf)
        self.assertLessEqual(seen['left'], views.RESOLVE_OVERPASS_BUDGET_S)

    def test_the_budget_fits_inside_the_worker_timeout(self):
        """If this ever fails, the unit file and the app disagree and the
        worker gets killed mid-request again."""
        from pathlib import Path
        unit = (Path(__file__).resolve().parents[2]
                / 'deploy' / 'box' / 'toponymia.service').read_text()
        match = re.search(r'--timeout (\d+)', unit)
        self.assertIsNotNone(match, 'gunicorn --timeout is not set')
        self.assertGreater(
            int(match.group(1)), views.RESOLVE_OVERPASS_BUDGET_S
        )

    @patch('core.resolve.overpass.fetch_elements')
    def test_a_management_caller_gets_no_budget(self, fetch):
        import math
        seen = {}

        def record(*args, **kwargs):
            seen['left'] = overpass._seconds_left()
            return []

        fetch.side_effect = record
        from core import resolve as resolution
        resolution.resolve('Ojai', 'city', -119.2, 34.4, 12)
        self.assertEqual(seen['left'], math.inf)

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

    @patch('core.resolve.overpass.fetch_element_name')
    @patch('core.resolve.overpass.fetch_elements')
    def test_osm_ref_recovers_a_localized_geocoder_name(self, fetch, el_name):
        # Photon answers in the browser's language, so a search for
        # Brașov arrives as 'Brasov' and the `name` query finds nothing.
        # The ref names the element; its own name finds it.
        fetch.side_effect = [[], [_relation(name='Brașov', qid='Q82174')]]
        el_name.return_value = 'Brașov'
        place = self._post(
            name='Brasov', lngLat=[25.6106, 45.6525],
            osm_ref='relation/10367676', **{'class': 'city'},
        ).json()['place']
        el_name.assert_called_once_with('relation', '10367676')
        self.assertEqual(place['anchor_level'], 'wikidata')
        self.assertEqual(place['wikidata_qid'], 'Q82174')

    @patch('core.resolve.overpass.fetch_element_name')
    @patch('core.resolve.overpass.fetch_elements')
    def test_osm_ref_costs_nothing_when_the_name_matches(self, fetch, el_name):
        fetch.return_value = [_relation(name='Brașov', qid='Q82174')]
        place = self._post(
            name='Brașov', lngLat=[25.6106, 45.6525],
            osm_ref='relation/10367676', **{'class': 'city'},
        ).json()['place']
        # The extra request is the fallback's whole cost, so it must not
        # be spent on the path that already works.
        el_name.assert_not_called()
        self.assertEqual(place['wikidata_qid'], 'Q82174')

    @patch('core.resolve.overpass.fetch_element_name')
    @patch('core.resolve.overpass.fetch_elements')
    def test_unnamed_osm_ref_still_reaches_the_name_anchor(self, fetch,
                                                           el_name):
        fetch.return_value = []
        el_name.return_value = None
        place = self._post(
            name='Nowhere', osm_ref='way/5', **{'class': 'city'}
        ).json()['place']
        self.assertEqual(place['anchor_level'], 'name')

    def test_rejects_malformed_osm_ref(self):
        for ref in ('12345', 'node/', 'chunk/12', 'node/12; drop'):
            self.assertEqual(self._post(osm_ref=ref).status_code, 400)


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
                'etymologies': [
                    {'etymology_md': '*test* + *-ville*'},
                ],
            },
        ],
    }
    content.update(overrides)
    return content


def _primary(content, index=0):
    """The first name's leading etymology — where prose, source languages,
    elements and references live."""
    return content['names'][index]['etymologies'][0]


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
        _primary(content)['from_languages'] = ['lat', 'Latin']
        response = self._put(content=content)
        self.assertEqual(response.status_code, 400)
        self.assertIn('Latin', str(response.json()))
        self.assertEqual(Revision.objects.count(), 0)

    def test_language_codes_normalized_to_iso639_3(self):
        # ISO 639-1 two-letter input is accepted and stored as 639-3
        self.client.force_login(self.user)
        content = _content()
        content['names'][0]['language'] = 'FR'
        _primary(content)['from_languages'] = ['la', 'ang']
        response = self._put(content=content)
        self.assertEqual(response.status_code, 200)
        stored = response.json()['article']['content']['names'][0]
        self.assertEqual(stored['language'], 'fra')
        self.assertEqual(stored['etymologies'][0]['from_languages'],
                         ['lat', 'ang'])
        row = PlaceName.objects.get(place=self.place)
        self.assertEqual(row.language, 'fra')
        self.assertEqual(row.from_languages, ['lat', 'ang'])

    def test_element_language_codes_normalized_too(self):
        # The etymon table is the point of the schema, so its language
        # column has to be dataset-grade like the others, not free text.
        self.client.force_login(self.user)
        content = _content()
        _primary(content)['elements'] = [
            {'form': 'aqua', 'language': 'la', 'gloss': 'water',
             'role': 'generic'},
        ]
        response = self._put(content=content)
        self.assertEqual(response.status_code, 200)
        stored = response.json()['article']['content']['names'][0]
        self.assertEqual(stored['etymologies'][0]['elements'][0]['language'],
                         'lat')

    def test_unknown_element_language_rejected(self):
        self.client.force_login(self.user)
        content = _content()
        _primary(content)['elements'] = [
            {'form': 'aqua', 'language': 'Latin'},
        ]
        self.assertEqual(self._put(content=content).status_code, 400)
        self.assertEqual(Revision.objects.count(), 0)

    def test_element_needs_only_a_form(self):
        # An editor who knows the word but not the taxonomy must not be
        # blocked — everything except `form` defaults to blank.
        self.client.force_login(self.user)
        content = _content()
        _primary(content)['elements'] = [{'form': 'nemos'}]
        response = self._put(content=content)
        self.assertEqual(response.status_code, 200)
        element = (
            response.json()['article']['content']
            ['names'][0]['etymologies'][0]['elements'][0]
        )
        self.assertEqual(element['form'], 'nemos')
        self.assertEqual(element['role'], '')
        self.assertEqual(element['gloss'], '')

    def test_bad_element_role_rejected(self):
        self.client.force_login(self.user)
        content = _content()
        _primary(content)['elements'] = [
            {'form': 'nemos', 'role': 'prefixish'},
        ]
        self.assertEqual(self._put(content=content).status_code, 400)

    def test_confidence_choices_enforced(self):
        self.client.force_login(self.user)
        content = _content()
        _primary(content)['confidence'] = 'probably-made-up'
        self.assertEqual(self._put(content=content).status_code, 400)

    def test_confidence_defaults_to_unset_not_unknown(self):
        # '' means nobody said; 'unknown' asserts that scholarship doesn't
        # know. Collapsing them would poison the label.
        self.client.force_login(self.user)
        response = self._put()
        self.assertEqual(response.status_code, 200)
        stored = response.json()['article']['content']['names'][0]
        self.assertEqual(stored['etymologies'][0]['confidence'], '')

    def test_competing_etymologies_round_trip(self):
        self.client.force_login(self.user)
        content = _content()
        content['names'][0]['etymologies'] = [
            {'etymology_md': 'From Gaulish.', 'confidence': 'probable',
             'from_languages': ['xtg']},
            {'etymology_md': 'Sometimes said to honour a king.',
             'confidence': 'folk', 'from_languages': ['lat']},
        ]
        response = self._put(content=content)
        self.assertEqual(response.status_code, 200)
        stored = response.json()['article']['content']['names'][0]
        self.assertEqual(len(stored['etymologies']), 2)
        self.assertEqual(stored['etymologies'][1]['confidence'], 'folk')

    def test_source_languages_materialize_as_a_union(self):
        # PlaceName.from_languages feeds map filtering and search, which
        # want recall: a disputed Latin hypothesis should still make the
        # place findable under Latin.
        self.client.force_login(self.user)
        content = _content()
        content['names'][0]['etymologies'] = [
            {'etymology_md': 'a', 'from_languages': ['xtg', 'lat']},
            {'etymology_md': 'b', 'from_languages': ['lat', 'ang']},
        ]
        self.assertEqual(self._put(content=content).status_code, 200)
        row = PlaceName.objects.get(place=self.place)
        # Primary first, deduplicated, order preserved.
        self.assertEqual(row.from_languages, ['xtg', 'lat', 'ang'])

    def test_too_many_etymologies_rejected(self):
        self.client.force_login(self.user)
        content = _content()
        content['names'][0]['etymologies'] = [
            {'etymology_md': f'theory {i}'}
            for i in range(MAX_ETYMOLOGIES + 1)
        ]
        self.assertEqual(self._put(content=content).status_code, 400)
        self.assertEqual(Revision.objects.count(), 0)

    def test_too_many_elements_rejected(self):
        self.client.force_login(self.user)
        content = _content()
        _primary(content)['elements'] = [
            {'form': f'w{i}'} for i in range(MAX_ELEMENTS + 1)
        ]
        self.assertEqual(self._put(content=content).status_code, 400)
        self.assertEqual(Revision.objects.count(), 0)

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
        _primary(content)['etymology_md'] = 'x' * (MAX_MARKDOWN + 1)
        response = self._put(content=content)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Revision.objects.count(), 0)

    def test_etymology_at_the_limit_accepted(self):
        self.client.force_login(self.user)
        content = _content()
        _primary(content)['etymology_md'] = 'x' * MAX_MARKDOWN
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
        _primary(content)['references'] = [
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
        _primary(oversized)['etymology_md'] = 'x' * (MAX_MARKDOWN + 500)
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
        self.assertEqual(
            len(restored['etymologies'][0]['etymology_md']),
            MAX_MARKDOWN + 500,
        )


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

    def _thread_count(self):
        """The count the pane puts on the Talk tab."""
        return self.client.get(
            reverse('core:place-detail', args=[self.place.slug])
        ).json()['talk_thread_count']

    def test_thread_count_starts_at_zero(self):
        self.assertEqual(self._thread_count(), 0)

    def test_thread_count_follows_the_listing(self):
        _talk(self.place, self.user)
        _talk(self.place, self.user)
        self.assertEqual(self._thread_count(), 2)

    def test_deleted_thread_leaves_the_count(self):
        post = _talk(self.place, self.user)
        post.thread.deleted = timezone.now()
        post.thread.save(update_fields=['deleted'])
        self.assertEqual(self._thread_count(), 0)

    def test_tombstoned_thread_still_counts(self):
        # Its posts are deleted but the thread still lists, as a tombstone
        # — so the number over the list has to agree with the list.
        _talk(self.place, self.user, deleted=timezone.now())
        self.assertEqual(self._thread_count(), 1)
        threads = self.client.get(
            reverse('core:talk', args=[self.place.slug])
        ).json()['threads']
        self.assertEqual(len(threads), 1)

    def test_thread_count_ignores_other_places(self):
        _talk(_make_place('Elsewhere', 'elsewhere'), self.user)
        self.assertEqual(self._thread_count(), 0)

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


def _talk(place, author, **overrides):
    """Open a discussion on a place, making it a wanted page if unwritten."""
    thread = TalkThread.objects.create(place=place, title='Where from?')
    fields = {'thread': thread, 'author': author, 'body_md': 'hi'}
    fields.update(overrides)
    return TalkPost.objects.create(**fields)


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

    def test_place_without_article_or_talk_excluded(self):
        _make_place()
        body = self._get().json()
        self.assertEqual(body['type'], 'FeatureCollection')
        self.assertEqual(body['features'], [])

    def test_article_tier_tagged(self):
        _publish(_make_place(), self.user)
        feature = self._get().json()['features'][0]
        self.assertEqual(feature['properties']['kind'], 'article')

    def test_discussed_stub_is_a_wanted_page(self):
        # Talk attaches to the Place, so a discussion can precede the
        # article. That place is a highlight in its own right — the
        # hollow-dot/halo tier — rather than being lost in the sea of
        # unwritten places.
        _talk(_make_place(), self.user)
        feature = self._get().json()['features'][0]
        self.assertEqual(feature['properties']['slug'], 'testville')
        self.assertEqual(feature['properties']['kind'], 'talk')

    def test_article_wins_over_talk_on_the_same_place(self):
        # The article is the stronger claim; its talk is one tab away.
        place = _make_place()
        _publish(place, self.user)
        _talk(place, self.user)
        features = self._get().json()['features']
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]['properties']['kind'], 'article')

    def test_empty_thread_is_not_a_wanted_page(self):
        # A thread with no posts is nobody discussing anything.
        TalkThread.objects.create(place=_make_place(), title='Where from?')
        self.assertEqual(self._get().json()['features'], [])

    def test_deleted_talk_drops_out_of_highlights(self):
        # Both soft deletes, mirroring how a deleted article leaves the
        # article tier: the map must not keep advertising a discussion
        # that no visitor can read.
        buried = _talk(_make_place(slug='buried'), self.user)
        buried.thread.deleted = timezone.now()
        buried.thread.save(update_fields=['deleted'])
        _talk(_make_place(), self.user, deleted=timezone.now())
        self.assertEqual(self._get().json()['features'], [])

    def test_deleted_article_falls_back_to_the_talk_tier(self):
        # Soft-deleting the article leaves a stub — but the discussion on
        # it is still live and still worth finding.
        place = _make_place()
        _publish(place, self.user)
        _talk(place, self.user)
        place.article.deleted = timezone.now()
        place.article.save(update_fields=['deleted'])
        feature = self._get().json()['features'][0]
        self.assertEqual(feature['properties']['kind'], 'talk')

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


class ContributionsApiTests(ApiTestCase):
    """The "your contributions" map lens. Unlike highlights this is not
    viewport-scoped: it answers "where have I been?", so it ships the
    user's whole footprint plus a box to frame it with."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('drew', password='pw12345!')
        self.other = User.objects.create_user('sam', password='pw12345!')

    def _get(self):
        return self.client.get(reverse('core:contributions'))

    def test_requires_sign_in(self):
        self.assertEqual(self._get().status_code, 403)

    def test_empty_for_a_user_who_has_written_nothing(self):
        _publish(_make_place(), self.other)
        self.client.force_login(self.user)
        body = self._get().json()
        self.assertEqual(body['features'], [])
        # No dots means nothing to frame — the client shows the empty
        # state rather than flying the camera at a made-up box.
        self.assertIsNone(body['bbox'])
        self.assertFalse(body['truncated'])

    def test_includes_places_the_user_edited(self):
        _publish(_make_place(), self.user)
        _publish(_make_place('Elsewhere', 'elsewhere'), self.other)
        self.client.force_login(self.user)
        features = self._get().json()['features']
        self.assertEqual(
            [f['properties']['slug'] for f in features], ['testville']
        )

    def test_includes_an_earlier_edit_someone_else_has_since_revised(self):
        # Authorship of *any* revision counts, not just the current one:
        # the article is still somewhere you've been.
        place = _make_place()
        _publish(place, self.user)
        article = place.article
        article.current_revision = Revision.objects.create(
            article=article, author=self.other, comment='', content=_content()
        )
        article.save(update_fields=['current_revision'])
        self.client.force_login(self.user)
        self.assertEqual(len(self._get().json()['features']), 1)

    def test_includes_a_stub_the_user_only_talked_on(self):
        # Talk attaches to the Place, not the Article, so a discussion
        # about a place nobody has written up yet still earns a dot.
        _talk(_make_place(), self.user)
        self.client.force_login(self.user)
        features = self._get().json()['features']
        self.assertEqual(
            [f['properties']['slug'] for f in features], ['testville']
        )

    def test_excludes_a_place_only_others_talked_on(self):
        _talk(_make_place(), self.other)
        self.client.force_login(self.user)
        self.assertEqual(self._get().json()['features'], [])

    def test_excludes_deleted_talk_posts_and_threads(self):
        withheld = _talk(
            _make_place('Gone', 'gone'), self.user, deleted=timezone.now()
        )
        self.assertIsNotNone(withheld)
        buried = _talk(_make_place('Buried', 'buried'), self.user)
        TalkThread.objects.filter(pk=buried.thread_id).update(
            deleted=timezone.now()
        )
        self.client.force_login(self.user)
        self.assertEqual(self._get().json()['features'], [])

    def test_excludes_an_edit_whose_article_was_deleted(self):
        # `published_places`'s rule, held to in this listing too: a
        # deleted article doesn't linger anywhere public.
        place = _make_place()
        _publish(place, self.user)
        Article.objects.filter(place=place).update(deleted=timezone.now())
        self.client.force_login(self.user)
        self.assertEqual(self._get().json()['features'], [])

    def test_counts_a_place_once_however_many_times_it_was_touched(self):
        place = _make_place()
        _publish(place, self.user)
        article = place.article
        Revision.objects.create(
            article=article, author=self.user, comment='', content=_content()
        )
        _talk(place, self.user)
        self.client.force_login(self.user)
        self.assertEqual(len(self._get().json()['features']), 1)

    def test_tags_a_written_place_as_an_article_dot(self):
        _publish(_make_place(), self.user)
        self.client.force_login(self.user)
        features = self._get().json()['features']
        self.assertEqual([f['properties']['kind'] for f in features],
                         ['article'])

    def test_tags_a_place_you_only_talked_on_as_a_wanted_page(self):
        _talk(_make_place(), self.user)
        self.client.force_login(self.user)
        features = self._get().json()['features']
        self.assertEqual([f['properties']['kind'] for f in features], ['talk'])

    def test_the_tier_is_about_the_article_not_about_your_role(self):
        # You only argued here; someone else wrote it up. That's a filled
        # dot, because the ring means "nobody has written this" on every
        # layer — it doesn't quietly mean "you only talked" on this one.
        place = _make_place()
        _talk(place, self.user)
        _publish(place, self.other)
        self.client.force_login(self.user)
        features = self._get().json()['features']
        self.assertEqual([f['properties']['kind'] for f in features],
                         ['article'])

    def test_bbox_spans_every_dot(self):
        _publish(_make_place(), self.user)  # (10, 50)
        east = Place.objects.create(
            slug='eastward',
            anchor_level=Place.AnchorLevel.NAME,
            display_name='Eastward',
            feature_class='city',
            centroid=Point(30.0, 55.0, srid=4326),
        )
        _publish(east, self.user)
        self.client.force_login(self.user)
        self.assertEqual(self._get().json()['bbox'], [10.0, 50.0, 30.0, 55.0])

    def test_flags_a_footprint_past_the_cap(self):
        with patch('core.views.MAX_CONTRIBUTIONS', 1):
            _publish(_make_place(), self.user)
            _publish(_make_place('Elsewhere', 'elsewhere'), self.user)
            self.client.force_login(self.user)
            body = self._get().json()
        # Truncated, and honest about it — the client says the view is
        # partial rather than quietly dropping the tail.
        self.assertEqual(len(body['features']), 1)
        self.assertTrue(body['truncated'])


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

    def test_signups_are_open_by_default(self):
        """The flag defaults off, so nothing about dev or a normal deploy
        changes — and /api/me/ says so to anyone, signed in or not."""
        self.assertTrue(self.client.get(reverse('core:me')).json()['signups_open'])

    @override_settings(PRELAUNCH=True)
    def test_prelaunch_closes_signups(self):
        """The window between "the box serves the site" and "the site is open"
        has to be *empty*, not merely quiet.

        Seed content is loaded by restoring a dump, which replaces the whole
        database. An account created in that window would be destroyed by the
        import along with its revisions — and TERMS.md §2 makes revision
        history the attribution mechanism for the CC BY-SA grant, so that is a
        broken licence promise, not untidy data. allauth checks this before
        creating anything, so there is no half-made user to clean up.
        """
        response = self.client.post(
            '/_allauth/browser/v1/auth/signup',
            {
                'username': 'stranger',
                'email': 'stranger@example.com',
                'password': 'sturdy-passphrase-9',
                'terms': True,
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username='stranger').exists())
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(
            self.client.get(reverse('core:me')).json()['signups_open']
        )

    @override_settings(PRELAUNCH=True)
    def test_prelaunch_leaves_existing_accounts_alone(self):
        """Closed registration, not a closed site: the seeding account and the
        superuser both have to keep working through the window."""
        user = User.objects.create_user(
            'topobot', email='bot@example.com', password='sturdy-passphrase-9'
        )
        # Verified, because mandatory verification would otherwise 401 this
        # login for reasons that have nothing to do with the flag under test.
        EmailAddress.objects.create(
            user=user, email='bot@example.com', verified=True, primary=True
        )
        response = self.client.post(
            '/_allauth/browser/v1/auth/login',
            {'username': 'topobot', 'password': 'sturdy-passphrase-9'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(reverse('core:me')).json()['user']['username'],
            'topobot',
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

    def test_signup_with_an_existing_email_does_not_500(self):
        """allauth answers a signup for a known address by sending "you
        already have an account" rather than admitting the address is taken —
        and that mail links to the reset flow, which needs
        HEADLESS_FRONTEND_URLS['account_reset_password'].

        HEADLESS_ONLY raises rather than defaulting, so the key being absent
        turned every signup by a returning user into a 500. The response must
        also stay indistinguishable from a fresh signup, or the enumeration
        resistance it exists for is gone.
        """
        existing = User.objects.create_user(
            'taken', email='taken@example.com', password='pw12345!'
        )
        EmailAddress.objects.create(
            user=existing,
            email='taken@example.com',
            verified=True,
            primary=True,
        )

        def signup(username, email):
            return self.client.post(
                '/_allauth/browser/v1/auth/signup',
                {
                    'username': username,
                    'email': email,
                    'password': 'sturdy-passphrase-9',
                    'terms': True,
                },
                content_type='application/json',
            )

        taken = signup('fresh', 'taken@example.com')
        self.assertEqual(taken.status_code, 401)
        # No second account, and the address owner is told.
        self.assertFalse(User.objects.filter(username='fresh').exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['taken@example.com'])

        self.client.logout()
        fresh = signup('fresh2', 'fresh2@example.com')
        self.assertEqual(fresh.status_code, taken.status_code)
        self.assertEqual(fresh.json(), taken.json())

    def test_signup_with_an_existing_username_is_refused_plainly(self):
        """The deliberate asymmetry with the test above.

        Usernames are public on this wiki — they sign every revision and talk
        post — so there is nothing to protect by hiding that one is taken, and
        a vague error would only strand someone at the signup form. Email is
        different, and is handled differently.
        """
        User.objects.create_user('taken', password='pw12345!')
        response = self.client.post(
            '/_allauth/browser/v1/auth/signup',
            {
                'username': 'taken',
                'email': 'fresh@example.com',
                'password': 'sturdy-passphrase-9',
                'terms': True,
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        codes = {e['code'] for e in response.json()['errors']}
        self.assertIn('username_taken', codes)

    def test_account_email_is_branded_not_hostnamed(self):
        """The verification mail is the first thing a new account ever sees.

        allauth builds both the greeting and the subject prefix from
        `current_site.name`, and with django.contrib.sites deliberately not
        installed that name is the request's *host* — so unbranded output
        here means the override in templates/ or the subject-prefix setting
        has been lost, not merely that the wording changed.
        """
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
        message = mail.outbox[0]
        self.assertTrue(message.subject.startswith('[Toponymia] '))
        self.assertIn('Hello from Toponymia!', message.body)
        self.assertNotIn('testserver', message.subject)
        self.assertNotIn('Hello from testserver', message.body)

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

    def test_skips_the_place_already_open(self):
        for place in (_make_place(), _make_place('Otherton', 'otherton')):
            _publish(place, self.user)
        body = self.client.get(
            reverse('core:random'), {'not': 'testville'}
        ).json()['place']
        self.assertEqual(body['slug'], 'otherton')

    def test_nothing_left_once_the_open_place_is_skipped(self):
        # A one-article wiki with that article open: null, and the button
        # sits still rather than reopening what's already there.
        _publish(_make_place(), self.user)
        body = self.client.get(
            reverse('core:random'), {'not': 'testville'}
        ).json()['place']
        self.assertIsNone(body)

    def test_unknown_slug_excludes_nothing(self):
        _publish(_make_place(), self.user)
        body = self.client.get(
            reverse('core:random'), {'not': 'nowhere'}
        ).json()['place']
        self.assertEqual(body['slug'], 'testville')


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

    # --- the reported marker -----------------------------------------
    def _talk_posts(self):
        threads = self.client.get(
            reverse('core:talk', args=[self.place.slug])
        ).json()['threads']
        return [p for t in threads for p in t['posts']]

    def _history(self):
        return self.client.get(
            reverse('core:revision-list', args=[self.place.slug])
        ).json()['revisions']

    def test_reported_marker_is_false_before_reporting(self):
        self._thread_with_post()
        self.client.force_login(self.other)
        self.assertFalse(self._talk_posts()[0]['reported'])

    def test_reported_marker_is_true_for_the_reporter(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        self._report('talk_post', post['id'])
        self.assertTrue(self._talk_posts()[0]['reported'])

    def test_reported_marker_is_private_to_the_reporter(self):
        """The whole point of viewer-relative: a report must not be visible
        to the reported author, to a bystander, or to a moderator reading the
        thread — only in the queue."""
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        self._report('talk_post', post['id'])
        third = User.objects.create_user('kit', password='pw12345!')
        for viewer in (self.author, third, self.mod):
            self.client.force_login(viewer)
            self.assertFalse(
                self._talk_posts()[0]['reported'], f'leaked to {viewer}'
            )

    def test_reported_marker_is_false_when_logged_out(self):
        post = self._thread_with_post()[1]
        self.client.force_login(self.other)
        self._report('talk_post', post['id'])
        self.client.logout()
        self.assertFalse(self._talk_posts()[0]['reported'])

    def test_reported_marker_survives_the_report_being_closed(self):
        """Permanence is the part that stops a second filing, so a dismissal
        must not hand the button back."""
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
        self.client.force_login(self.other)
        self.assertTrue(self._talk_posts()[0]['reported'])

    def test_refiling_after_a_dismissal_creates_nothing(self):
        """The hole the marker's permanence has to be backed by: the open-only
        unique constraints let a closed report be filed again, once per
        dismissal, each one a fresh queue row and a fresh moderator email."""
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
        self.client.force_login(self.other)
        mail.outbox = []
        again = self._report('talk_post', post['id'])
        # 201 and the original report back: the reporter is not told their
        # complaint was refused, and nothing new reaches the queue.
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.json()['report']['id'], report.id)
        self.assertEqual(Report.objects.count(), 1)
        self.assertEqual(Report.objects.get().status, Report.Status.DISMISSED)
        self.assertEqual(len(mail.outbox), 0)

    def test_reported_marker_on_revisions(self):
        revision = self._revision()
        self.client.force_login(self.other)
        self.assertFalse(self._history()[0]['reported'])
        self._report('revision', revision.id)
        self.assertTrue(self._history()[0]['reported'])
        detail = self.client.get(
            reverse('core:revision-detail', args=[self.place.slug,
                                                  revision.id])
        ).json()['revision']
        self.assertTrue(detail['reported'])

    def test_reported_marker_costs_one_query_per_page(self):
        """A thread can carry eighty posts; the marker must not cost a query
        each. Two renders of different sizes taking the same number of
        queries is the property that matters, not the absolute count."""
        thread = self._thread_with_post()[0]
        self.client.force_login(self.other)
        with CaptureQueriesContext(connection) as one_post:
            self._talk_posts()
        self.client.force_login(self.author)
        for i in range(5):
            self.client.post(
                reverse('core:talk-reply', args=[thread['id']]),
                {'body_md': f'reply {i}'},
                content_type='application/json',
            )
        self.client.force_login(self.other)
        with CaptureQueriesContext(connection) as six_posts:
            posts = self._talk_posts()
        self.assertEqual(len(posts), 6)
        self.assertEqual(len(six_posts), len(one_post))

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

    def test_suppressed_revision_is_a_public_tombstone(self):
        """The row survives for attribution; its content does not.

        CC BY-SA credit lives in the history, and a suppressed author's
        earlier prose may still be in the live article, so the byline has to
        outlast the removal. Everything readable goes.
        """
        old = self._older_revision()
        old.comment = 'a slur in the edit summary'
        old.suppressed = timezone.now()
        old.suppressed_by = self.mod
        old.save(update_fields=['comment', 'suppressed', 'suppressed_by'])
        url = reverse('core:revision-list', args=[self.place.slug])

        rows = self.client.get(url).json()['revisions']
        row = next(r for r in rows if r['id'] == old.id)
        self.assertTrue(row['suppressed'])
        self.assertEqual(row['author'], old.author.username)
        self.assertEqual(row['created'], old.created.isoformat())
        self.assertEqual(row['comment'], '')  # withheld

        # moderator: the same row, with the summary intact
        self.client.force_login(self.mod)
        rows = self.client.get(url).json()['revisions']
        row = next(r for r in rows if r['id'] == old.id)
        self.assertEqual(row['comment'], 'a slur in the edit summary')

    def test_suppressed_revision_counts_in_public_pagination(self):
        # A history that silently dropped rows would also announce, by the
        # gap in it, exactly which revisions were hidden.
        old = self._older_revision()
        url = reverse('core:revision-list', args=[self.place.slug])
        before = self.client.get(url).json()['total']
        old.suppressed = timezone.now()
        old.save(update_fields=['suppressed'])
        self.assertEqual(self.client.get(url).json()['total'], before)

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

    # --- removed threads ---------------------------------------------
    def _thread_by(self, user, title='t'):
        thread = TalkThread.objects.create(place=self.place, title=title)
        TalkPost.objects.create(thread=thread, author=user, body_md='hello')
        return thread

    def _row_for(self, username='drew'):
        rows = self.client.get(reverse('core:mod-users')).json()['users']
        return next((r for r in rows if r['username'] == username), None)

    def test_removed_thread_counts_against_whoever_started_it(self):
        # A thread has no author column; the opening post's author is who
        # answers for it. Without this the deletion showed up nowhere.
        thread = self._thread_by(self.author)
        TalkPost.objects.create(
            thread=thread, author=self.reporter, body_md='replying'
        )
        self.client.force_login(self.mod)
        self.client.delete(
            reverse('core:talk-thread-delete', args=[thread.id])
        )
        self.assertEqual(self._row_for('drew')['removed_count'], 1)
        # The replier isn't answerable for a thread they only joined.
        self.assertIsNone(self._row_for('sam'))

    def test_live_thread_counts_against_nobody(self):
        self._thread_by(self.author)
        self.client.force_login(self.mod)
        self.assertIsNone(self._row_for('drew'))

    def test_user_detail_lists_threads_started_with_removal_state(self):
        thread = self._thread_by(self.author, title='Where from?')
        self.client.force_login(self.mod)
        self.client.delete(
            reverse('core:talk-thread-delete', args=[thread.id])
        )
        detail = self.client.get(
            reverse('core:mod-user-detail', args=[self.author.id])
        ).json()
        self.assertEqual(len(detail['talk_threads']), 1)
        row = detail['talk_threads'][0]
        self.assertEqual(row['title'], 'Where from?')
        self.assertEqual(row['post_count'], 1)
        self.assertTrue(row['deleted'])

    def test_user_detail_omits_threads_the_user_only_replied_to(self):
        thread = self._thread_by(self.author)
        TalkPost.objects.create(
            thread=thread, author=self.reporter, body_md='replying'
        )
        self.client.force_login(self.mod)
        detail = self.client.get(
            reverse('core:mod-user-detail', args=[self.reporter.id])
        ).json()
        self.assertEqual(detail['talk_threads'], [])

    def test_mod_restores_a_deleted_thread(self):
        thread = self._thread_by(self.author)
        self.client.force_login(self.mod)
        self.client.delete(
            reverse('core:talk-thread-delete', args=[thread.id])
        )
        response = self.client.post(
            reverse('core:mod-talk-thread-restore', args=[thread.id])
        )
        self.assertEqual(response.status_code, 200)
        thread.refresh_from_db()
        self.assertIsNone(thread.deleted)
        self.assertIsNone(thread.deleted_by)
        # And it's back in the public conversation, not merely un-flagged.
        page = self.client.get(
            reverse('core:talk', args=[self.place.slug])
        ).json()
        self.assertEqual([t['id'] for t in page['threads']], [thread.id])

    def test_restoring_a_thread_leaves_its_deleted_posts_removed(self):
        # Deleting the thread hid the conversation wholesale; undoing that
        # shouldn't quietly undo the narrower judgements inside it.
        thread = self._thread_by(self.author)
        buried = TalkPost.objects.create(
            thread=thread, author=self.reporter, body_md='spam',
            deleted=timezone.now(),
        )
        self.client.force_login(self.mod)
        self.client.delete(
            reverse('core:talk-thread-delete', args=[thread.id])
        )
        self.client.post(
            reverse('core:mod-talk-thread-restore', args=[thread.id])
        )
        buried.refresh_from_db()
        self.assertIsNotNone(buried.deleted)

    def test_thread_restore_requires_moderator(self):
        thread = self._thread_by(self.author)
        TalkThread.objects.filter(pk=thread.pk).update(deleted=timezone.now())
        self.client.force_login(self.reporter)
        self.assertEqual(
            self.client.post(
                reverse('core:mod-talk-thread-restore', args=[thread.id])
            ).status_code,
            403,
        )

    def test_thread_removal_and_restore_land_in_the_users_audit_trail(self):
        thread = self._thread_by(self.author)
        self.client.force_login(self.mod)
        self.client.delete(
            reverse('core:talk-thread-delete', args=[thread.id])
        )
        self.client.post(
            reverse('core:mod-talk-thread-restore', args=[thread.id])
        )
        detail = self.client.get(
            reverse('core:mod-user-detail', args=[self.author.id])
        ).json()
        self.assertEqual(
            [a['action'] for a in detail['audit']],
            ['restore_thread', 'delete_thread'],
        )

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
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('core:mod-ban-user', args=[self.author.id]),
            {'reason': 'x', 'remove_content': True},
            content_type='application/json',
        )
        self.assertEqual(response.json()['removed_content']['talk_posts'], 1)
        post.refresh_from_db()
        self.assertIsNotNone(post.deleted)

    # --- ban with content removal ------------------------------------
    #
    # The scenario the checkbox exists for: someone posts something nobody
    # should have to read, and the ban is supposed to take it down. Until
    # M14 it took down everything *except* the text that was actually on
    # screen, because an article's current revision can't be suppressed
    # without leaving the article blank. These cover the three ways out.

    def _remove_all(self, actor=None):
        self.client.force_login(actor or self.admin)
        response = self.client.post(
            reverse('core:mod-ban-user', args=[self.author.id]),
            {'reason': 'abuse', 'remove_content': True},
            content_type='application/json',
        )
        self.client.logout()
        return response

    def test_removal_reverts_an_article_off_the_banned_users_edit(self):
        clean = save_edit(
            self.place, self.reporter, _content(body_md='clean text'), 'ok'
        )
        save_edit(
            self.place, self.author, _content(body_md='SLUR'), 'bad'
        )
        removed = self._remove_all().json()['removed_content']

        self.assertEqual(removed['articles_reverted'], 1)
        article = Article.objects.get(place=self.place)
        self.assertEqual(
            article.current_revision.content['body_md'], 'clean text'
        )
        # The revert is a new revision by the acting admin, not a rewrite of
        # history, and the abusive one is now suppressed history.
        self.assertEqual(article.current_revision.author, self.admin)
        self.assertNotEqual(article.current_revision_id, clean.id)
        self.assertFalse(
            Revision.objects.filter(
                author=self.author, suppressed__isnull=True
            ).exists()
        )

    def test_removal_deletes_an_article_only_the_banned_user_wrote(self):
        save_edit(self.place, self.author, _content(body_md='SLUR'), 'bad')
        removed = self._remove_all().json()['removed_content']

        self.assertEqual(removed['articles_deleted'], 1)
        self.assertEqual(removed['articles_reverted'], 0)
        article = Article.objects.get(place=self.place)
        self.assertIsNotNone(article.deleted)
        self.assertEqual(article.deleted_by, self.admin)
        # With the article down there is nothing to keep textful, so even the
        # current revision is suppressed — otherwise a later edit to the place
        # would quietly demote it to history and re-expose it.
        self.assertIsNotNone(
            Revision.objects.get(id=article.current_revision_id).suppressed
        )

    def test_removal_takes_the_text_out_of_the_public_article(self):
        save_edit(self.place, self.author, _content(body_md='SLUR'), 'bad')
        self._remove_all()
        data = self.client.get(
            reverse('core:place-detail', args=[self.place.slug])
        ).json()
        self.assertNotIn('SLUR', json.dumps(data))

    def test_removal_takes_the_text_out_of_search_and_the_map(self):
        # Names are materialized from the *current* revision into PlaceName,
        # which feeds the search box and the map highlights — so an abusive
        # name outlives a removal that only touches revisions.
        save_edit(
            self.place, self.author,
            _content(names=[{
                'name': 'Slurville', 'language': 'eng',
                'is_endonym': True,
                'etymologies': [{'etymology_md': 'x'}],
            }]),
            'bad',
        )
        self.assertTrue(PlaceName.objects.filter(name='Slurville').exists())
        self._remove_all()

        results = self.client.get(
            reverse('core:search'), {'q': 'Slurville'}
        ).json()['results']
        self.assertEqual(results, [])
        self.assertFalse(PlaceName.objects.filter(name='Slurville').exists())

    def test_removal_hides_the_edit_summary_too(self):
        save_edit(self.place, self.reporter, _content(), 'ok')
        save_edit(
            self.place, self.author, _content(body_md='x'), 'SLUR IN SUMMARY'
        )
        self._remove_all()
        rows = self.client.get(
            reverse('core:revision-list', args=[self.place.slug])
        ).json()['revisions']
        self.assertNotIn('SLUR IN SUMMARY', json.dumps(rows))

    def test_removal_keeps_the_byline_for_attribution(self):
        # Their earlier prose can survive in the live article under a later
        # editor's revision; CC BY-SA credit for it lives in this row.
        save_edit(self.place, self.author, _content(body_md='useful'), 'good')
        save_edit(self.place, self.reporter, _content(body_md='useful'), 'ok')
        self._remove_all()
        rows = self.client.get(
            reverse('core:revision-list', args=[self.place.slug])
        ).json()['revisions']
        self.assertIn('drew', [r['author'] for r in rows])

    def test_removal_masks_the_talk_byline_publicly(self):
        post = self._post_by(self.author, body='SLUR')
        self._remove_all()
        threads = self.client.get(
            reverse('core:talk', args=[self.place.slug])
        ).json()['threads']
        row = next(
            p for t in threads for p in t['posts'] if p['id'] == post.id
        )
        self.assertEqual(row['author'], '[deleted]')
        self.assertEqual(row['body_md'], '')
        # A moderator still sees who wrote it.
        self.client.force_login(self.mod)
        threads = self.client.get(
            reverse('core:talk', args=[self.place.slug])
        ).json()['threads']
        row = next(
            p for t in threads for p in t['posts'] if p['id'] == post.id
        )
        self.assertEqual(row['author'], 'drew')

    def test_removal_is_admin_only(self):
        save_edit(self.place, self.author, _content(body_md='SLUR'), 'bad')
        response = self._remove_all(actor=self.mod)
        self.assertEqual(response.status_code, 403)
        # And the ban didn't half-apply.
        self.assertFalse(Ban.objects.filter(user=self.author).exists())
        self.assertIsNone(Article.objects.get(place=self.place).deleted)

    def test_plain_ban_leaves_all_content_alone(self):
        save_edit(self.place, self.author, _content(body_md='fine'), 'ok')
        post = self._post_by(self.author)
        self.client.force_login(self.mod)
        response = self.client.post(
            reverse('core:mod-ban-user', args=[self.author.id]),
            {'reason': 'x'}, content_type='application/json',
        )
        self.assertIsNone(response.json()['removed_content'])
        post.refresh_from_db()
        self.assertIsNone(post.deleted)
        article = Article.objects.get(place=self.place)
        self.assertIsNone(article.deleted)
        self.assertEqual(article.current_revision.content['body_md'], 'fine')
        self.assertIsNone(article.current_revision.suppressed)

    def test_restore_refused_while_current_revision_is_suppressed(self):
        # The state bulk removal leaves behind: restoring here would put the
        # removed text straight back on the page.
        save_edit(self.place, self.author, _content(body_md='SLUR'), 'bad')
        self._remove_all()
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('core:article-restore', args=[self.place.slug])
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(Article.objects.get(place=self.place).deleted)

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

    @override_settings(PRELAUNCH=True)
    def test_robots_closes_the_site_before_launch(self):
        """A certificate publishes the hostname to Certificate Transparency
        logs the moment Caddy issues one, so crawlers arrive without anything
        being announced — and a half-seeded wiki is what they would index.
        The sitemap goes too: advertising every URL would undercut the
        Disallow it sits next to."""
        body = self.client.get('/robots.txt').content.decode()
        self.assertIn('Disallow: /\n', body)
        self.assertNotIn('Sitemap:', body)


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
        row = next(
            r for r in self.client.get(
                reverse('core:revision-list', args=[self.place.slug])
            ).json()['revisions']
            if r['id'] == self.revision.id
        )
        # The row is back in public history — the article was rewritten, so
        # the history is public again — but it is still a tombstone: the
        # byline stands, the summary and the snapshot don't.
        self.assertTrue(row['suppressed'])
        self.assertEqual(row['comment'], '')
        self.assertEqual(
            self.client.get(
                reverse(
                    'core:revision-detail',
                    args=[self.place.slug, self.revision.id],
                )
            ).status_code,
            404,
        )


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
                'etymologies': [{'etymology_md': 'rewritten'}],
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

    def test_feed_filters_by_action_group(self):
        # The dashboard's "Removed" filter is a group of kinds, sent as one
        # param so `total` counts the same rows the page shows.
        self.client.force_login(self.mod)
        body = self.client.get(
            reverse('core:mod-audit'), {'action': 'ban_user,delete_post'}
        ).json()
        self.assertEqual(body['total'], 2)
        self.assertEqual(len(body['actions']), 2)

    def test_feed_rejects_unknown_action_in_group(self):
        # One bad member fails the whole group rather than being dropped —
        # silently widening a filter would misreport what was matched.
        self.client.force_login(self.mod)
        response = self.client.get(
            reverse('core:mod-audit'), {'action': 'ban_user,wizardry'}
        )
        self.assertEqual(response.status_code, 400)

    def test_feed_filters_by_target(self):
        self.client.force_login(self.mod)
        rows = self.client.get(
            reverse('core:mod-audit'), {'target': self.other.id}
        ).json()['actions']
        self.assertEqual(len(rows), 2)
        rows = self.client.get(
            reverse('core:mod-audit'), {'target': self.mod.id}
        ).json()['actions']
        self.assertEqual(rows, [])

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

    # --- paging ------------------------------------------------------
    def _fill(self, count):
        """`count` more actions, so the feed spans several pages."""
        ModAction.objects.bulk_create([
            ModAction(
                actor=self.mod, action=ModAction.Action.DELETE_POST,
                target_user=self.other, reason=f'row {n}',
            )
            for n in range(count)
        ])

    def test_feed_reports_its_own_size_and_position(self):
        self.client.force_login(self.mod)
        body = self.client.get(reverse('core:mod-audit')).json()
        self.assertEqual(body['total'], 2)
        self.assertEqual(body['offset'], 0)
        self.assertEqual(body['page_size'], dashboard.AUDIT_PAGE)

    def test_offset_pages_through_without_repeating_a_row(self):
        self._fill(dashboard.AUDIT_PAGE)
        self.client.force_login(self.mod)
        first = self.client.get(reverse('core:mod-audit')).json()
        second = self.client.get(
            reverse('core:mod-audit'), {'offset': dashboard.AUDIT_PAGE}
        ).json()
        self.assertEqual(len(first['actions']), dashboard.AUDIT_PAGE)
        self.assertEqual(len(second['actions']), 2)
        self.assertEqual(second['offset'], dashboard.AUDIT_PAGE)
        ids = [a['id'] for a in first['actions'] + second['actions']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), first['total'])

    def test_offset_past_the_end_clamps_to_the_last_rows(self):
        # An empty page is indistinguishable from "nothing matches", so a
        # page number past the end lands on real rows instead.
        self.client.force_login(self.mod)
        body = self.client.get(
            reverse('core:mod-audit'), {'offset': 9999}
        ).json()
        self.assertEqual(body['offset'], 1)
        self.assertEqual(len(body['actions']), 1)

    def test_offset_on_an_empty_feed_stays_at_zero(self):
        ModAction.objects.all().delete()
        self.client.force_login(self.mod)
        body = self.client.get(
            reverse('core:mod-audit'), {'offset': 40}
        ).json()
        self.assertEqual(body['total'], 0)
        self.assertEqual(body['offset'], 0)
        self.assertEqual(body['actions'], [])

    def test_feed_rejects_a_bad_offset(self):
        self.client.force_login(self.mod)
        for value in ('abc', '-1'):
            with self.subTest(offset=value):
                self.assertEqual(
                    self.client.get(
                        reverse('core:mod-audit'), {'offset': value}
                    ).status_code,
                    400,
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


class ReportNotificationTests(ApiTestCase):
    """Mail sent when a report is filed. The queue is the source of truth;
    these tests are about the notification never getting in its way."""

    def setUp(self):
        super().setUp()
        self.place = _make_place()
        self.author = User.objects.create_user('drew', password='pw12345!')
        self.reporter = User.objects.create_user('sam', password='pw12345!')
        self.mod = User.objects.create_user(
            'mira', password='pw12345!', is_staff=True, email='mira@x.test'
        )
        self.client.force_login(self.author)
        thread = self.client.post(
            reverse('core:talk', args=[self.place.slug]),
            {'title': 'Etymology dispute', 'body_md': 'Sources?'},
            content_type='application/json',
        ).json()['thread']
        self.post_id = thread['posts'][0]['id']
        self.client.force_login(self.reporter)
        mail.outbox = []

    def _report(self, reason='pasted from a book', category='copyright'):
        return self.client.post(
            reverse('core:report-create'),
            {
                'target_type': 'talk_post',
                'target_id': self.post_id,
                'category': category,
                'reason': reason,
            },
            content_type='application/json',
        )

    def test_filing_a_report_mails_moderators(self):
        self.assertEqual(self._report().status_code, 201)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        # BCC, not To: moderators shouldn't learn each other's addresses.
        self.assertEqual(message.to, [])
        self.assertEqual(message.bcc, ['mira@x.test'])
        self.assertIn('Copyright', message.subject)
        self.assertIn('sam', message.body)
        self.assertIn('pasted from a book', message.body)
        self.assertIn(f'/place/{self.place.slug}', message.body)

    def test_refiling_is_silent(self):
        self._report()
        self._report()
        # Idempotent at the model, so the second POST creates nothing — and
        # must therefore mail nothing.
        self.assertEqual(Report.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_reporter_is_not_mailed_about_their_own_report(self):
        self.reporter.is_staff = True
        self.reporter.email = 'sam@x.test'
        self.reporter.save(update_fields=['is_staff', 'email'])
        self._report()
        self.assertEqual(mail.outbox[0].bcc, ['mira@x.test'])

    def test_moderators_without_an_address_are_skipped(self):
        self.mod.email = ''
        self.mod.save(update_fields=['email'])
        self._report()
        self.assertEqual(mail.outbox, [])

    def test_ceiling_sends_a_pause_notice_then_nothing(self):
        # Fill the window to one below the ceiling with reports from other
        # accounts, so the one under test is the Nth.
        for index in range(notify.REPORT_MAIL_CEILING - 1):
            Report.objects.create(
                reporter=User.objects.create_user(f'filer{index}'),
                talk_post_id=self.post_id,
                category=Report.Category.SPAM,
            )
        self._report()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('paused', mail.outbox[0].subject)

        mail.outbox = []
        self.client.force_login(User.objects.create_user('late'))
        self._report()
        self.assertEqual(mail.outbox, [])
        # Past the ceiling the report is still recorded — only the mail stops.
        self.assertEqual(
            Report.objects.count(), notify.REPORT_MAIL_CEILING + 1
        )

    def test_old_reports_do_not_count_towards_the_ceiling(self):
        old = timezone.now() - notify.REPORT_MAIL_WINDOW - timedelta(minutes=1)
        for index in range(notify.REPORT_MAIL_CEILING + 5):
            report = Report.objects.create(
                reporter=User.objects.create_user(f'filer{index}'),
                talk_post_id=self.post_id,
                category=Report.Category.SPAM,
            )
            Report.objects.filter(pk=report.pk).update(created=old)
        self._report()
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn('paused', mail.outbox[0].subject)

    def test_smtp_failure_does_not_break_the_report(self):
        # No task queue, so this send is inline on the request. A dead mail
        # host must cost the notification, not the report.
        with patch(
            'core.notify.EmailMessage.send', side_effect=OSError('no smtp')
        ):
            with self.assertLogs('core.notify', level='ERROR'):
                response = self._report()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Report.objects.count(), 1)


class ReportOutcomeMailTests(ApiTestCase):
    """Mail sent to the *reporter* when a moderator closes their report.

    The report notification's constraints all apply again — inline send, no
    queue, failure isolated — plus one of its own: this is the only mail the
    site sends to an ordinary user about someone else's content, so it must
    not carry the content or the moderator's note.
    """

    def setUp(self):
        super().setUp()
        self.place = _make_place()
        self.author = User.objects.create_user('drew', password='pw12345!')
        self.reporter = User.objects.create_user(
            'sam', password='pw12345!', email='sam@x.test'
        )
        self.mod = User.objects.create_user(
            'mira', password='pw12345!', is_staff=True, email='mira@x.test'
        )
        self.client.force_login(self.author)
        thread = self.client.post(
            reverse('core:talk', args=[self.place.slug]),
            {'title': 'Etymology dispute', 'body_md': 'Sources?'},
            content_type='application/json',
        ).json()['thread']
        self.post_id = thread['posts'][0]['id']
        self.client.force_login(self.reporter)
        self.client.post(
            reverse('core:report-create'),
            {'target_type': 'talk_post', 'target_id': self.post_id,
             'category': 'harassment', 'reason': 'they called me a fool'},
            content_type='application/json',
        )
        self.report = Report.objects.get()
        mail.outbox = []

    def _act(self, action, reason='', actor=None):
        self.client.force_login(actor or self.mod)
        return self.client.post(
            reverse('core:mod-report-action', args=[self.report.id]),
            {'action': action, 'reason': reason},
            content_type='application/json',
        )

    def test_removal_tells_the_reporter(self):
        self.assertEqual(self._act('delete').status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        # To, not BCC: one-to-one mail with an empty To reads as a blast.
        self.assertEqual(message.to, ['sam@x.test'])
        self.assertEqual(message.bcc, [])
        self.assertIn('removed the content', message.body)
        self.assertIn(f'/place/{self.place.slug}', message.body)

    def test_dismissal_tells_the_reporter_and_closes_the_subject(self):
        self._act('dismiss')
        body = mail.outbox[0].body
        self.assertIn('does not break the site rules', body)
        self.assertIn('not be taking further action', body)

    def test_resolve_credits_the_report_without_claiming_a_removal(self):
        """`resolve` covers "handled elsewhere" as well as "looked at and
        closed", so it must not promise the content came down — while still
        saying plainly that something was done, because the reporter may be
        looking at the content as they read it."""
        self._act('resolve')
        body = mail.outbox[0].body
        self.assertIn('took action', body)
        self.assertIn('may still see the content', body)
        self.assertNotIn('removed the content', body)

    def test_outcome_mail_carries_neither_the_content_nor_the_mod_note(self):
        self._act('delete', reason='obvious sock puppet, watch this account')
        body = mail.outbox[0].body
        self.assertNotIn('sock puppet', body)
        self.assertNotIn('Sources?', body)
        # Nor the reporter's own words back at them.
        self.assertNotIn('fool', body)

    def test_moderator_acting_on_their_own_report_is_not_mailed(self):
        self.report.reporter = self.mod
        self.report.save(update_fields=['reporter'])
        self._act('dismiss')
        self.assertEqual(mail.outbox, [])

    def test_reporter_without_an_address_is_skipped(self):
        self.reporter.email = ''
        self.reporter.save(update_fields=['email'])
        self._act('delete')
        self.assertEqual(mail.outbox, [])

    def test_deactivated_reporter_is_skipped(self):
        self.reporter.is_active = False
        self.reporter.save(update_fields=['is_active'])
        self._act('delete')
        self.assertEqual(mail.outbox, [])

    def test_smtp_failure_does_not_break_the_moderator_action(self):
        with patch(
            'core.notify.EmailMessage.send', side_effect=OSError('no smtp')
        ):
            with self.assertLogs('core.notify', level='ERROR'):
                response = self._act('delete')
        # The decision stands even though the reporter never hears about it.
        self.assertEqual(response.status_code, 200)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Report.Status.RESOLVED)
        self.assertIsNotNone(TalkPost.objects.get(id=self.post_id).deleted)

    def test_suppressing_a_revision_tells_the_reporter(self):
        """The revision path: a different target type, and its own wording."""
        article = Article.objects.create(place=self.place)
        first = Revision.objects.create(
            article=article, author=self.author, comment='draft',
            content=_content(),
        )
        current = Revision.objects.create(
            article=article, author=self.author, comment='more',
            content=_content(),
        )
        article.current_revision = current
        article.save(update_fields=['current_revision'])
        Report.objects.all().delete()
        self.client.force_login(self.reporter)
        self.client.post(
            reverse('core:report-create'),
            {'target_type': 'revision', 'target_id': first.id,
             'category': 'copyright', 'reason': 'copied'},
            content_type='application/json',
        )
        self.report = Report.objects.get()
        mail.outbox = []
        self.assertEqual(self._act('suppress').status_code, 200)
        self.assertIn('from public view', mail.outbox[0].body)
        self.assertIn(f'/place/{self.place.slug}', mail.outbox[0].body)


def _admin_box(subdivision, country, west, south, east, north, **kwargs):
    """One rectangular admin area. Fixtures, deliberately not real data —
    load_admin_boundaries never runs in tests, so the table would otherwise
    be empty and every qualifier assertion would pass vacuously."""
    return AdminArea.objects.create(
        subdivision=subdivision,
        subdivision_local=kwargs.pop('local', subdivision),
        country=country,
        geometry=MultiPolygon(
            Polygon.from_bbox((west, south, east, north)), srid=4326
        ),
        **kwargs,
    )


class AdminQualifierTests(TestCase):
    """Which admin area qualifies a point (core.admin_areas). Two adjacent
    boxes sharing the 46th parallel, plus a subdivision that shares its name
    with the place inside it."""

    @classmethod
    def setUpTestData(cls):
        _admin_box('Oregon', 'United States of America',
                   -124.0, 42.0, -117.0, 46.0, wikidata_qid='Q824',
                   subdivision_type='State')
        _admin_box('Washington', 'United States of America',
                   -124.0, 46.0, -117.0, 49.0)
        # Jamaica's first-order divisions are parishes, one of which is
        # named Portland — the case that forces the country fall-through.
        _admin_box('Portland', 'Jamaica', -76.9, 17.9, -76.1, 18.4,
                   subdivision_type='Parish')
        # A microstate: its one subdivision shares the country's name.
        _admin_box('Monaco', 'Monaco', 10.0, 10.0, 11.0, 11.0)

    def _qualify(self, lng, lat, name, qid=None, feature_class=None):
        from .admin_areas import qualifier_for
        return qualifier_for(
            Point(lng, lat, srid=4326), name, qid, feature_class
        )

    def test_containing_subdivision_qualifies(self):
        self.assertEqual(
            self._qualify(-122.7, 45.5, 'Portland'), 'oregon'
        )

    def test_neighbouring_subdivision_is_not_borrowed(self):
        self.assertEqual(
            self._qualify(-122.7, 47.5, 'Vancouver'), 'washington'
        )

    def test_point_just_inside_a_border(self):
        # 45.99 is ~1 km south of the Oregon/Washington line.
        self.assertEqual(self._qualify(-120.0, 45.99, 'Ojai'), 'oregon')

    def test_point_exactly_on_a_border_still_qualifies(self):
        # Contained by neither box under a strict predicate; both are at
        # distance 0, so the KNN ordering just has to pick one.
        self.assertIn(
            self._qualify(-120.0, 46.0, 'Ojai'), {'oregon', 'washington'}
        )

    def test_point_outside_but_within_tolerance(self):
        # 41.9 is ~11 km south of Oregon's edge — a stand-in for the
        # generalised coastlines that leave Tromsø 1.6 km offshore.
        self.assertEqual(self._qualify(-120.0, 41.9, 'Coos Bay'), 'oregon')

    def test_point_far_from_everything_is_unqualified(self):
        self.assertIsNone(self._qualify(0.0, 0.0, 'Nowhere'))

    def test_empty_table_qualifies_nothing_and_does_not_raise(self):
        AdminArea.objects.all().delete()
        self.assertIsNone(self._qualify(-122.7, 45.5, 'Portland'))

    def test_self_reference_falls_through_to_the_country(self):
        # Portland Parish contains Port Antonio; a place *named* Portland
        # there must not mint `portland-portland`.
        self.assertEqual(
            self._qualify(-76.5, 18.2, 'Portland'), 'jamaica'
        )

    def test_self_reference_matches_on_qid_not_just_spelling(self):
        # The state of Oregon itself, minted from its own QID.
        self.assertEqual(
            self._qualify(-120.0, 44.0, 'State of Oregon', 'Q824'), 'usa'
        )

    def test_self_reference_at_both_rungs_gives_nothing(self):
        self.assertIsNone(self._qualify(10.5, 10.5, 'Monaco'))

    def test_a_country_is_never_qualified_by_its_own_subdivision(self):
        # Minting the USA itself from a click in Oregon must not give
        # `united-states-of-america-oregon`; nothing contains a country.
        self.assertIsNone(
            self._qualify(-120.0, 44.0, 'United States of America')
        )

    def test_country_self_reference_catches_the_alias_too(self):
        self.assertIsNone(self._qualify(-120.0, 44.0, 'USA'))

    def test_country_alias_shortens_the_formal_name(self):
        self.assertEqual(self._qualify(-120.0, 44.0, 'Oregon'), 'usa')

    def test_admin_area_sharing_its_name_takes_its_type(self):
        # Havana: the province is named after the city and sits on top of
        # it, so no geography can separate them. The province is typed…
        # …and with the country's own word, not the tile schema's: Jamaica
        # has parishes, so `portland-parish`, never `portland-county`.
        self.assertEqual(
            self._qualify(-76.5, 18.2, 'Portland', feature_class='county'),
            'parish',
        )

    def test_type_falls_back_to_the_clicked_class_without_ne_data(self):
        # ~8% of NE rows carry no type at all.
        AdminArea.objects.filter(country='Jamaica').update(subdivision_type='')
        self.assertEqual(
            self._qualify(-76.5, 18.2, 'Portland', feature_class='county'),
            'county',
        )

    def test_the_settlement_twin_is_not_typed(self):
        # …while the city falls through to the ordinary ladder, so it can
        # still hold the bare slug. This is the asymmetry that makes the
        # pair independent of mint order.
        self.assertEqual(
            self._qualify(-76.5, 18.2, 'Portland', feature_class='city'),
            'jamaica',
        )

    def test_typing_needs_the_name_to_actually_match(self):
        # An ordinary county inside Oregon is qualified by Oregon, not by
        # the word 'county' — the type rung is only for the twin case.
        self.assertEqual(
            self._qualify(-120.0, 44.0, 'Multnomah', feature_class='county'),
            'oregon',
        )

    def test_boundary_clicks_are_not_typed(self):
        # 'boundary' names the geometry, not the thing; `portland-boundary`
        # would tell a reader nothing, so it takes the ordinary ladder.
        self.assertEqual(
            self._qualify(-76.5, 18.2, 'Portland', feature_class='boundary'),
            'jamaica',
        )

    def test_type_rung_beats_the_country_for_a_state(self):
        self.assertEqual(
            self._qualify(-120.0, 44.0, 'Oregon', feature_class='state'),
            'state',
        )

    def test_pipe_joined_ne_types_take_the_first(self):
        # NE writes alternatives as 'Commune|Municipality'; the loader
        # keeps the primary, so the slug never grows a hyphenated pair.
        from .admin_areas import _fragment
        area = AdminArea.objects.get(country='Jamaica')
        area.subdivision_type = 'Commune'
        area.save(update_fields=['subdivision_type'])
        self.assertEqual(_fragment(area.subdivision_type), 'commune')

    def test_qualifier_is_transliterated(self):
        _admin_box('Troms', 'Norway', 18.0, 69.0, 20.0, 70.0)
        _admin_box('Ærø', 'Denmark', 10.2, 54.8, 10.5, 55.0)
        self.assertEqual(self._qualify(10.35, 54.9, 'Marstal'), 'aero')


class ResolveQualifiesTests(ApiTestCase):
    """The mint paths actually consult the boundary table: the slug gets a
    qualifier and the row keeps the admin context behind it."""

    @classmethod
    def setUpTestData(cls):
        _admin_box('Oregon', 'United States of America',
                   -124.0, 42.0, -117.0, 46.0, country_iso='US',
                   subdivision_iso='US-OR', subdivision_type='State')
        _admin_box('Maine', 'United States of America',
                   -71.1, 43.0, -66.9, 47.5, country_iso='US',
                   subdivision_iso='US-ME', subdivision_type='State')

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('qualifier', password='pw12345!')
        self.client.force_login(self.user)

    def _post(self, **overrides):
        payload = {
            'name': 'Portland',
            'class': 'city',
            'lngLat': [-122.6, 45.5],
            'zoom': 12,
        }
        payload.update(overrides)
        return self.client.post(
            reverse('core:resolve'), payload, content_type='application/json'
        )

    @patch('core.resolve.overpass.fetch_elements')
    def test_second_namesake_is_qualified_by_its_state(self, fetch):
        fetch.return_value = []
        first = self._post(
            name='Portland', **{'class': 'city'}, lngLat=[-122.6, 45.5]
        ).json()['place']
        second = self._post(
            name='Portland', **{'class': 'city'}, lngLat=[-70.2, 43.6]
        ).json()['place']
        self.assertEqual(first['slug'], 'portland')
        self.assertEqual(second['slug'], 'portland-maine')

    @patch('core.resolve.overpass.fetch_elements')
    def test_admin_context_is_stored_even_without_a_collision(self, fetch):
        fetch.return_value = []
        slug = self._post(
            name='Ojai', **{'class': 'city'}, lngLat=[-122.6, 45.5]
        ).json()['place']['slug']
        place = Place.objects.get(slug=slug)
        self.assertEqual(place.admin_subdivision, 'Oregon')
        self.assertEqual(place.admin_subdivision_iso, 'US-OR')
        self.assertEqual(place.admin_country, 'United States of America')
        self.assertEqual(place.admin_country_iso, 'US')

    @patch('core.resolve.overpass.fetch_elements')
    def test_context_blank_when_nothing_contains_the_place(self, fetch):
        fetch.return_value = []
        slug = self._post(
            name='Nowhere', **{'class': 'city'}, lngLat=[0.0, 0.0]
        ).json()['place']['slug']
        place = Place.objects.get(slug=slug)
        self.assertEqual(place.admin_subdivision, '')
        self.assertEqual(place.admin_country, '')

    @patch('core.resolve.overpass.fetch_way_component', return_value=None)
    @patch('core.resolve.overpass.fetch_way_geometry')
    @patch('core.resolve.overpass.fetch_elements')
    def test_element_is_qualified_from_label_point_not_the_click(
        self, fetch, fetch_geom, fetch_comp
    ):
        """A worldwide fetch_by_qid match can sit nowhere near the click.

        The click here is in Maine and the way is in Oregon; the slug must
        follow the feature, not the finger.
        """
        way = {'type': 'way', 'id': 77, 'tags': {'name': 'Portland'},
               'bounds': {'minlat': 45.4, 'minlon': -122.8,
                          'maxlat': 45.6, 'maxlon': -122.5}}
        fetch.return_value = [way]
        fetch_geom.return_value = [(-122.7, 45.45), (-122.6, 45.55)]
        _make_place(name='Portland', slug='portland')
        place = self._post(
            name='Portland', **{'class': 'city'}, lngLat=[-70.2, 43.6]
        ).json()['place']
        self.assertEqual(place['slug'], 'portland-oregon')
        self.assertEqual(
            Place.objects.get(slug='portland-oregon').admin_subdivision,
            'Oregon',
        )


def _area(name, admin_level, boundary='administrative', **tags):
    """One `is_in` result, shaped as Overpass returns it: type 'area',
    tags only, no geometry and no bounds."""
    return {
        'type': 'area',
        'id': 3600000000 + abs(hash(name)) % 1000000,
        'tags': {'name': name, 'admin_level': str(admin_level),
                 'boundary': boundary, **tags},
    }


def _parish(name):
    """An English civil parish — the village, at level 10."""
    return _area(name, 10, designation='civil_parish')


def _place_area(name, kind='town'):
    """A settlement polygon: `boundary=place`, no admin_level at all."""
    return {
        'type': 'area',
        'id': 3600000000 + abs(hash(name)) % 1000000,
        'tags': {'name': name, 'boundary': 'place', 'place': kind},
    }


class ResolveLocalityRungTests(ApiTestCase):
    """The locality rung end to end: what contains the feature lands in
    the slug, at a scale that matches how far the feature reaches."""

    @classmethod
    def setUpTestData(cls):
        _admin_box('Lincolnshire', 'United Kingdom',
                   -1.0, 52.6, 0.4, 53.6, country_iso='GB',
                   subdivision_type='County')
        _admin_box('Oregon', 'United States of America',
                   -124.0, 42.0, -117.0, 46.0, country_iso='US',
                   subdivision_type='State')

    # Boston and Lincoln both sit in Lincolnshire — the pair that minted
    # `high-street-lincolnshire` and `high-street-lincolnshire-2` in
    # production on 2026-08-18.
    BOSTON = [_area('United Kingdom', 2), _area('Lincolnshire', 6),
              _area('Boston', 8)]
    LINCOLN = [_area('United Kingdom', 2), _area('Lincolnshire', 6),
               _area('Lincoln', 8)]

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('cityrung', password='pw12345!')
        self.client.force_login(self.user)

    def _post(self, name, lng, lat, feature_class='road'):
        return self.client.post(
            reverse('core:resolve'),
            {'name': name, 'class': feature_class,
             'lngLat': [lng, lat], 'zoom': 15},
            content_type='application/json',
        ).json()['place']['slug']

    @patch('core.resolve.overpass.fetch_elements')
    def test_two_high_streets_in_one_county(self, fetch):
        """The regression this whole rung exists for."""
        fetch.return_value = list(self.BOSTON)
        self.assertEqual(
            self._post('High Street', -0.0264, 52.9788), 'high-street'
        )
        fetch.return_value = list(self.LINCOLN)
        self.assertEqual(
            self._post('High Street', -0.5405, 53.2258),
            'high-street-lincoln',
        )

    @patch('core.resolve.overpass.fetch_elements')
    def test_third_one_is_named_too_rather_than_numbered(self, fetch):
        fetch.return_value = list(self.BOSTON)
        self._post('High Street', -0.0264, 52.9788)
        fetch.return_value = list(self.LINCOLN)
        self._post('High Street', -0.5405, 53.2258)
        fetch.return_value = [_area('United Kingdom', 2),
                              _area('Lincolnshire', 6),
                              _area('Sleaford', 8)]
        self.assertEqual(
            self._post('High Street', -0.41, 52.99), 'high-street-sleaford'
        )

    @patch('core.resolve.overpass.fetch_way_component')
    @patch('core.resolve.overpass.fetch_way_geometry')
    @patch('core.resolve.overpass.fetch_elements')
    def test_an_osm_anchored_street_takes_the_city_too(
        self, fetch, fetch_geom, fetch_comp
    ):
        # Not just the name-anchor path: a street that resolves to a real
        # way must be qualified the same way.
        fetch_comp.return_value = None
        fetch_geom.return_value = [(-0.026, 52.978), (-0.027, 52.979)]
        way = {'type': 'way', 'id': 91, 'tags': {'name': 'High Street'},
               'bounds': {'minlat': 52.978, 'minlon': -0.027,
                          'maxlat': 52.979, 'maxlon': -0.026}}
        _make_place(name='High Street', slug='high-street')
        fetch.return_value = [way, *self.BOSTON]
        self.assertEqual(
            self._post('High Street', -0.0264, 52.9788), 'high-street-boston'
        )

    @patch('core.resolve.overpass.fetch_way_component')
    @patch('core.resolve.overpass.fetch_way_geometry')
    @patch('core.resolve.overpass.fetch_elements')
    def test_a_distant_label_point_declines_the_city(
        self, fetch, fetch_geom, fetch_comp
    ):
        """The click's city is not the feature's city on a long feature.

        `is_in` answers about the click, but everything else here is
        qualified from label_point. A click on the Columbia near Portland
        whose label point is hundreds of km upstream must not mint
        `columbia-river-portland`.
        """
        fetch_comp.return_value = None
        fetch_geom.return_value = [(-119.0, 45.9), (-119.1, 45.95)]
        way = {'type': 'way', 'id': 92, 'tags': {'name': 'Columbia River'},
               'bounds': {'minlat': 45.9, 'minlon': -119.1,
                          'maxlat': 45.95, 'maxlon': -119.0}}
        _make_place(name='Columbia River', slug='columbia-river')
        fetch.return_value = [
            way, _area('United States of America', 2), _area('Oregon', 4),
            _area('Portland', 8),
        ]
        # Click in Portland; the way is ~430 km east.
        self.assertEqual(
            self._post('Columbia River', -122.67, 45.52), 'columbia-river-oregon'
        )

    @patch('core.resolve.overpass.fetch_elements')
    def test_a_city_click_falls_through_to_the_state(self, fetch):
        # Portland the city sits in Portland the admin area, so the rung
        # stands aside and the proven `portland-oregon` still mints.
        fetch.return_value = [_area('United States of America', 2),
                              _area('Oregon', 4), _area('Portland', 8)]
        _make_place(name='Portland', slug='portland')
        self.assertEqual(
            self._post('Portland', -122.67, 45.52, feature_class='city'),
            'portland-oregon',
        )

    @patch('core.resolve.overpass.fetch_elements')
    @patch('core.resolve.overpass.fetch_by_qid')
    def test_a_qid_mint_never_sees_a_city(self, fetch_qid, fetch):
        """topobot's path is a worldwide lookup with no point to ask
        about, so it keeps exactly today's Natural Earth qualifier."""
        fetch_qid.return_value = [
            {'type': 'node', 'id': 5, 'lat': 45.52, 'lon': -122.67,
             'tags': {'name': 'Portland', 'wikidata': 'Q6106'}}
        ]
        _make_place(name='Portland', slug='portland')
        response = self.client.post(
            reverse('core:resolve'),
            {'name': 'Portland', 'class': 'city',
             'lngLat': [-122.67, 45.52], 'zoom': 12, 'qid': 'Q6106'},
            content_type='application/json',
        )
        self.assertEqual(
            response.json()['place']['slug'], 'portland-oregon'
        )
        fetch.assert_not_called()


class ContainingAreaTests(TestCase):
    """Reading the `is_in` half of a fetch_elements response
    (core.overpass). Fixtures are the real responses recorded 2026-08-18."""

    # Boston, Lincolnshire — the shape that produced the collision.
    BOSTON = [
        _area('United Kingdom', 2), _area('England', 4),
        _area('Greater Lincolnshire', 5), _area('Lincolnshire', 6),
        _area('Boston', 8),
    ]

    def test_splits_areas_from_features(self):
        way = _way(name='High Street')
        features, areas = overpass.split_areas([way, *self.BOSTON])
        self.assertEqual(features, [way])
        self.assertEqual(len(areas), 5)

    def test_split_is_a_no_op_without_areas(self):
        # The by-qid path sends no is_in, so every element is a feature.
        features, areas = overpass.split_areas([_relation()])
        self.assertEqual(len(features), 1)
        self.assertEqual(areas, [])

    def test_most_local_city_scale_area_wins(self):
        self.assertEqual(overpass.locality_name(self.BOSTON), 'Boston')

    def test_country_and_state_are_not_cities(self):
        # Levels 2 and 4 are what Natural Earth already gives us; taking
        # one here would make the rung a duplicate of the one below it.
        self.assertIsNone(
            overpass.locality_name([_area('United Kingdom', 2),
                                _area('England', 4)])
        )

    def test_neighbourhood_is_not_a_city(self):
        # Portland: level 10 'Downtown' is more local than the city, and
        # names nothing a reader can place.
        self.assertEqual(
            overpass.locality_name([
                _area('United States of America', 2), _area('Oregon', 4),
                _area('Multnomah County', 6), _area('Portland', 8),
                _area('Downtown', 10),
            ]),
            'Portland',
        )

    def test_scotland_has_no_level_eight(self):
        # City of Edinburgh is level 6 — the same rung as an English
        # county — which is why the band takes its max rather than a
        # fixed level.
        self.assertEqual(
            overpass.locality_name([
                _area('United Kingdom', 2), _area('Scotland', 4),
                _area('City of Edinburgh', 6), _area('Old Town', 10),
            ]),
            'City of Edinburgh',
        )

    def test_statistical_region_is_not_a_city(self):
        # 'East Central Scotland' is level 6 like the city it contains,
        # and would outrank nothing — but it is not an administrative
        # area, and the server-side filter is what keeps it out. Belt and
        # braces: a mirror that ignored the filter must not poison a slug.
        self.assertIsNone(
            overpass.locality_name([
                _area('East Central Scotland', 6, boundary='statistical'),
            ])
        )

    def test_untagged_level_is_ignored(self):
        self.assertIsNone(overpass.locality_name([
            {'type': 'area', 'id': 1, 'tags': {'name': 'Nowhere'}},
            {'type': 'area', 'id': 2, 'tags': {}},
        ]))

    def test_english_name_is_preferred(self):
        self.assertEqual(
            overpass.locality_name([_area('Wien', 6, **{'name:en': 'Vienna'})]),
            'Vienna',
        )


class LocalityQualifierTests(TestCase):
    """Turning a locality name into a slug fragment (core.admin_areas)."""

    @classmethod
    def setUpTestData(cls):
        cls.lincs = _admin_box('Lincolnshire', 'United Kingdom',
                               -1.0, 52.6, 0.4, 53.6)

    def _qualify(self, city, name, area=None):
        from .admin_areas import locality_qualifier
        return locality_qualifier(city, name, area)

    def test_city_becomes_a_fragment(self):
        self.assertEqual(self._qualify('Boston', 'High Street'), 'boston')

    def test_administrative_prefix_is_dropped(self):
        # `high-street-edinburgh`, which is the slug production already
        # minted for Edinburgh before this rung existed.
        self.assertEqual(
            self._qualify('City of Edinburgh', 'High Street'), 'edinburgh'
        )

    def test_parish_suffix_is_dropped(self):
        self.assertEqual(self._qualify('Ingham CP', 'Church Lane'), 'ingham')

    def test_town_council_suffix_is_dropped(self):
        # 'TC' is a parish whose council styles itself a town council —
        # governance, not a place. Caistor is the only one in England, so
        # `church-street-caistor-tc` reached production before anyone saw
        # it [Overpass, 2026-08-24].
        self.assertEqual(self._qualify('Caistor TC', 'Church Street'),
                         'caistor')

    def test_a_town_in_the_name_is_kept(self):
        # The counterpart: strip the governance suffix, never a word of
        # the name. The parish really is called Chard Town.
        self.assertEqual(self._qualify('Chard Town CP', 'High Street'),
                         'chard-town')

    def test_declines_when_it_would_repeat_the_place(self):
        # A click on Portland itself: the rung must stand aside so the
        # subdivision gives `portland-oregon`, not `portland-portland`.
        self.assertIsNone(self._qualify('Portland', 'Portland'))

    def test_declines_when_it_would_repeat_the_subdivision(self):
        # A city sharing its container's name makes the rung a duplicate
        # of the one below, and escalation would read
        # `high-street-lincolnshire-lincolnshire`.
        self.assertIsNone(
            self._qualify('Lincolnshire', 'High Street', self.lincs)
        )

    def test_declines_when_it_would_repeat_the_country(self):
        monaco = _admin_box('Monaco', 'Monaco', 10.0, 10.0, 11.0, 11.0)
        self.assertIsNone(self._qualify('Monaco', 'Rue Grimaldi', monaco))

    def test_no_city_is_no_fragment(self):
        self.assertIsNone(self._qualify(None, 'High Street'))

    def test_transliterates_like_every_other_fragment(self):
        self.assertEqual(self._qualify('Tromsø', 'Storgata'), 'tromso')


class LocalityRungSlugTests(TestCase):
    """unique_slug's ladder with a locality in it."""

    def _mint(self, name, qualifier=None, locality=None):
        from .slugs import unique_slug
        slug = unique_slug(name, qualifier, locality=locality)
        _make_place(name=name, slug=slug)
        return slug

    def test_first_place_still_keeps_the_bare_slug(self):
        self.assertEqual(
            self._mint('High Street', 'lincolnshire', locality='lincoln'),
            'high-street',
        )

    def test_city_is_tried_before_the_subdivision(self):
        self._mint('High Street', 'lincolnshire', locality='boston')
        self.assertEqual(
            self._mint('High Street', 'lincolnshire', locality='lincoln'),
            'high-street-lincoln',
        )

    def test_the_collision_this_rung_exists_for(self):
        """Two High Streets in one county, which used to give
        `high-street-lincolnshire` and `high-street-lincolnshire-2`."""
        self.assertEqual(
            self._mint('High Street', 'lincolnshire', locality='boston'),
            'high-street',
        )
        self.assertEqual(
            self._mint('High Street', 'lincolnshire', locality='lincoln'),
            'high-street-lincoln',
        )
        self.assertEqual(
            self._mint('High Street', 'lincolnshire', locality='sleaford'),
            'high-street-sleaford',
        )

    def test_escalation_stacks_city_and_subdivision(self):
        # Two Main Streets in two Portlands: the second gets the state
        # appended to the city, not swapped for it.
        self._mint('Main Street', 'oregon', locality='portland')
        self.assertEqual(
            self._mint('Main Street', 'oregon', locality='portland'),
            'main-street-portland',
        )
        self.assertEqual(
            self._mint('Main Street', 'maine', locality='portland'),
            'main-street-portland-maine',
        )

    def test_numeric_floor_counts_on_the_most_qualified_form(self):
        self._mint('Main Street', 'oregon', locality='portland')
        self._mint('Main Street', 'oregon', locality='portland')
        self._mint('Main Street', 'oregon', locality='portland')
        self.assertEqual(
            self._mint('Main Street', 'oregon', locality='portland'),
            'main-street-portland-oregon-2',
        )

    def test_no_city_is_exactly_todays_ladder(self):
        self._mint('Portland', 'oregon')
        self.assertEqual(
            self._mint('Portland', 'maine'), 'portland-maine'
        )


class NameableAreaTests(TestCase):
    """Which rungs of the containment stack may name a slug
    (core.overpass.locality_name)."""

    def test_civil_parish_is_the_village(self):
        """The 2026-08-19 regression: four Church Lanes in four West
        Lindsey villages all took the district and fell to `-3`.

        The village is level 10 here, below the band the first cut of
        this rung capped at.
        """
        self.assertEqual(
            overpass.locality_name([
                _area('United Kingdom', 2), _area('England', 4),
                _area('Lincolnshire', 6), _area('West Lindsey', 8),
                _parish('Ingham CP'),
            ]),
            'Ingham CP',
        )

    def test_neighbourhood_at_the_same_level_is_refused(self):
        """Level 10 alone proves nothing — it is the village in England
        and a neighbourhood in the US, and only a tag separates them."""
        self.assertEqual(
            overpass.locality_name([
                _area('United States of America', 2), _area('Oregon', 4),
                _area('Multnomah County', 6), _area('Portland', 8),
                _area('Downtown', 10),
            ]),
            'Portland',
        )

    def test_a_settlement_place_tag_also_admits_it(self):
        self.assertEqual(
            overpass.locality_name([
                _area('Somewhere', 8), _area('Little Bo', 10, place='village'),
            ]),
            'Little Bo',
        )

    def test_a_suburb_is_not_a_settlement(self):
        # `suburb` is in SETTLEMENT_PLACES but not TOWN_PLACES: naming a
        # street after part of a town is the outcome this guards against.
        self.assertEqual(
            overpass.locality_name([
                _area('Bigton', 8), _area('Northside', 10, place='suburb'),
            ]),
            'Bigton',
        )

    def test_country_and_state_are_still_refused(self):
        self.assertIsNone(
            overpass.locality_name([_area('United Kingdom', 2),
                                    _area('England', 4)])
        )


class CommonAreaQueryTests(TestCase):
    """fetch_common_areas asks Overpass to do the intersection."""

    @patch('core.overpass._call')
    def test_one_request_intersects_every_point(self, call):
        call.return_value = [_area('West Lindsey', 8)]
        points = [Point(-0.58, 53.28, srid=4326),
                  Point(-0.57, 53.31, srid=4326),
                  Point(-0.57, 53.34, srid=4326)]
        overpass.fetch_common_areas(points)
        self.assertEqual(call.call_count, 1)
        query = call.call_args[0][0]
        # One is_in per point, and a single intersected set out.
        self.assertEqual(query.count('is_in('), 3)
        self.assertIn('area.p0.p1.p2[boundary=administrative]', query)
        # Overpass takes lat,lon — transposing them silently qualifies
        # slugs from the wrong hemisphere.
        self.assertIn('is_in(53.28,-0.58)', query)

    @patch('core.overpass._call')
    def test_non_areas_are_dropped(self, call):
        call.return_value = [_way(name='High Street'), _area('Boston', 8)]
        areas = overpass.fetch_common_areas([Point(0.0, 52.0, srid=4326)])
        self.assertEqual([a['tags']['name'] for a in areas], ['Boston'])

    def test_no_points_makes_no_request(self):
        self.assertEqual(overpass.fetch_common_areas([]), [])


class ProbePointTests(TestCase):
    """Which points get asked about (core.resolve.probe_points)."""

    def test_start_middle_and_end(self):
        from .resolve import probe_points
        line = LineString([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)], srid=4326)
        points = probe_points(line)
        self.assertEqual([round(p.x, 6) for p in points], [0.0, 1.0, 2.0])

    def test_a_ring_needs_its_midpoint(self):
        """A ring road closes on itself, so its two ends are the same
        spot — the midpoint is the only probe that reveals its reach."""
        from .resolve import probe_points
        ring = LineString(
            [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)],
            srid=4326,
        )
        points = probe_points(ring)
        self.assertEqual((points[0].x, points[0].y),
                         (points[2].x, points[2].y))
        self.assertNotEqual((points[1].x, points[1].y),
                            (points[0].x, points[0].y))

    def test_a_point_has_no_course_to_walk(self):
        from .resolve import probe_points
        self.assertIsNone(probe_points(Point(0.0, 0.0, srid=4326)))
        self.assertIsNone(probe_points(None))


class ScaleMatchingTests(ApiTestCase):
    """The qualifier's scale follows the feature's extent."""

    @classmethod
    def setUpTestData(cls):
        _admin_box('Lincolnshire', 'United Kingdom',
                   -1.0, 52.6, 0.4, 53.6, country_iso='GB')

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('scale', password='pw12345!')
        self.client.force_login(self.user)

    def _road(self, name, coords, click):
        way = {'type': 'way', 'id': 55, 'tags': {'name': name},
               'bounds': {'minlon': min(c[0] for c in coords),
                          'minlat': min(c[1] for c in coords),
                          'maxlon': max(c[0] for c in coords),
                          'maxlat': max(c[1] for c in coords)}}
        return way

    def _post(self, name, lng, lat):
        return self.client.post(
            reverse('core:resolve'),
            {'name': name, 'class': 'road', 'lngLat': [lng, lat], 'zoom': 15},
            content_type='application/json',
        ).json()['place']['slug']

    @patch('core.resolve.overpass.fetch_common_areas')
    @patch('core.resolve.overpass.fetch_way_component')
    @patch('core.resolve.overpass.fetch_elements')
    def test_a_road_through_three_villages_takes_the_district(
        self, fetch, fetch_comp, fetch_common
    ):
        """No one village describes where this road is, so the area that
        holds all three does. The parishes drop out of the intersection
        server-side; here that is fetch_common_areas' answer."""
        coords = [(-0.58, 53.28), (-0.57, 53.31), (-0.57, 53.34)]
        fetch.return_value = [self._road('Church Lane', coords, None),
                              *[_parish('North Carlton CP')]]
        fetch_comp.return_value = [_component_way(55, coords, 'Church Lane')]
        fetch_common.return_value = [_area('Lincolnshire', 6),
                                     _area('West Lindsey', 8)]
        _make_place(name='Church Lane', slug='church-lane')
        self.assertEqual(
            self._post('Church Lane', -0.57, 53.31), 'church-lane-west-lindsey'
        )
        fetch_common.assert_called_once()
        # Start, middle and end — not just where the finger landed.
        self.assertEqual(len(fetch_common.call_args[0][0]), 3)

    @patch('core.resolve.overpass.fetch_common_areas')
    @patch('core.resolve.overpass.fetch_way_component')
    @patch('core.resolve.overpass.fetch_elements')
    def test_a_road_inside_one_village_takes_the_village(
        self, fetch, fetch_comp, fetch_common
    ):
        coords = [(-0.575, 53.339), (-0.570, 53.341), (-0.565, 53.345)]
        fetch.return_value = [self._road('Church Lane', coords, None),
                              _area('Lincolnshire', 6),
                              _area('West Lindsey', 8), _parish('Ingham CP')]
        fetch_comp.return_value = [_component_way(55, coords, 'Church Lane')]
        _make_place(name='Church Lane', slug='church-lane')
        self.assertEqual(
            self._post('Church Lane', -0.570, 53.341), 'church-lane-ingham'
        )
        # Under PROBE_MIN_EXTENT_M the click already answered it, so the
        # mint spends no second request.
        fetch_common.assert_not_called()

    @patch('core.resolve.overpass.fetch_common_areas')
    @patch('core.resolve.overpass.fetch_way_component')
    @patch('core.resolve.overpass.fetch_elements')
    def test_a_failed_intersection_mints_nothing(
        self, fetch, fetch_comp, fetch_common
    ):
        """It used to fall to Natural Earth and mint
        `church-lane-lincolnshire`, on the reasoning that a qualifier is a
        nicety and a mint is not. A qualifier is half of a permanent slug,
        so the mint waits for an answer it can trust."""
        coords = [(-0.58, 53.28), (-0.57, 53.31), (-0.57, 53.34)]
        fetch.return_value = [self._road('Church Lane', coords, None)]
        fetch_comp.return_value = [_component_way(55, coords, 'Church Lane')]
        fetch_common.side_effect = overpass.OverpassError('429')
        _make_place(name='Church Lane', slug='church-lane')
        response = self.client.post(
            reverse('core:resolve'),
            {'name': 'Church Lane', 'class': 'road',
             'lngLat': [-0.57, 53.31], 'zoom': 15},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Place.objects.filter(slug__startswith='church-lane-')
                         .exists())


class UnparishedTownTests(TestCase):
    """Settlement polygons, for the England that has no civil parish.

    Fixtures are the real Erewash response [Overpass, 2026-08-19]: the
    point sits in a district named after a river, and the town it is
    actually in arrives only as `boundary=place`.
    """

    EREWASH = [
        _area('United Kingdom', 2), _area('England', 4),
        _area('East Midlands', 5), _area('Derbyshire', 6),
        _area('Erewash', 8, designation='non_metropolitan_district'),
    ]

    def test_district_alone_names_a_river_not_a_town(self):
        # What shipped before this: Erewash is a district named after the
        # River Erewash and holds Ilkeston, Long Eaton and Sandiacre.
        self.assertEqual(overpass.locality_name(self.EREWASH), 'Erewash')

    def test_a_settlement_polygon_outranks_the_district(self):
        self.assertEqual(
            overpass.locality_name(
                [*self.EREWASH, _place_area('Ilkeston', 'town')]
            ),
            'Ilkeston',
        )

    def test_a_civil_parish_still_outranks_a_settlement_polygon(self):
        # The parish is the finer grain of the two, so where England has
        # one it keeps winning.
        self.assertEqual(
            overpass.locality_name([
                _area('Derbyshire Dales', 8), _place_area('Somewhere', 'town'),
                _parish('Brailsford CP'),
            ]),
            'Brailsford CP',
        )

    def test_a_suburb_polygon_is_refused(self):
        # `boundary=place` is not a licence: naming a street after part of
        # a town is the outcome TOWN_PLACES exists to prevent.
        self.assertEqual(
            overpass.locality_name(
                [*self.EREWASH, _place_area('Cotmanhay', 'suburb')]
            ),
            'Erewash',
        )

    def test_a_place_polygon_without_a_place_tag_is_refused(self):
        areas = [*self.EREWASH,
                 {'type': 'area', 'id': 9, 'tags': {'name': 'Mystery',
                                                    'boundary': 'place'}}]
        self.assertEqual(overpass.locality_name(areas), 'Erewash')


class AreaQueryUnionTests(TestCase):
    """Both queries ask for settlement polygons as well as admin areas."""

    @patch('core.overpass._call')
    def test_the_click_query_asks_for_both(self, call):
        call.return_value = []
        overpass.fetch_elements('Church Street', 52.99, -1.31, 60)
        query = call.call_args[0][0]
        self.assertIn('area.a[boundary=administrative]', query)
        self.assertIn('area.a[boundary=place]', query)

    @patch('core.overpass._call')
    def test_the_intersection_query_asks_for_both(self, call):
        call.return_value = []
        overpass.fetch_common_areas([Point(-1.31, 52.99, srid=4326),
                                     Point(-1.30, 52.99, srid=4326)])
        query = call.call_args[0][0]
        self.assertIn('area.p0.p1[boundary=administrative]', query)
        self.assertIn('area.p0.p1[boundary=place]', query)


class UnparishedResolveTests(ApiTestCase):
    """End to end: a street in an unparished town takes the town."""

    @classmethod
    def setUpTestData(cls):
        _admin_box('Derbyshire', 'United Kingdom',
                   -2.0, 52.7, -1.1, 53.5, country_iso='GB')

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('unparished', password='pw12345!')
        self.client.force_login(self.user)

    @patch('core.resolve.overpass.fetch_elements')
    def test_the_town_wins_over_the_district(self, fetch):
        fetch.return_value = [
            _area('Derbyshire', 6),
            _area('Erewash', 8, designation='non_metropolitan_district'),
            _place_area('Ilkeston', 'town'),
        ]
        _make_place(name='Church Street', slug='church-street')
        slug = self.client.post(
            reverse('core:resolve'),
            {'name': 'Church Street', 'class': 'road',
             'lngLat': [-1.31398, 52.99017], 'zoom': 15},
            content_type='application/json',
        ).json()['place']['slug']
        self.assertEqual(slug, 'church-street-ilkeston')


def _place_node(name, lat, lon, kind='village'):
    """A settlement node as `is_in`-adjacent queries return it."""
    return {'type': 'node', 'id': abs(hash(name)) % 10**7, 'lat': lat,
            'lon': lon, 'tags': {'name': name, 'place': kind}}


class PlaceNodeTests(TestCase):
    """The node rung: the only part of the ladder that infers membership
    from proximity rather than reading it off a boundary."""

    def test_nearest_wins_regardless_of_size(self):
        """A town three villages away must not beat the village you are in.

        This is the Romanian commune case that motivated the rung: the
        containing admin area is named after its seat, so preferring the
        larger `place` type would reproduce exactly the error.
        """
        nodes = [_place_node('Distant Town', 45.10, 24.10, 'town'),
                 _place_node('Right Here', 45.00, 24.00, 'village')]
        got = overpass.nearest_place_node(nodes, 45.001, 24.001)
        self.assertEqual(got[1], 'Right Here')

    def test_nothing_within_the_radius_declines(self):
        nodes = [_place_node('Far Away', 46.0, 25.0, 'town')]
        self.assertIsNone(overpass.nearest_place_node(nodes, 45.0, 24.0))

    def test_a_hamlet_or_suburb_is_not_accepted(self):
        # NODE_PLACES is narrower than the area rules: a dot is weak
        # evidence, so it is only trusted for a settlement of real size.
        nodes = [_place_node('Tiny', 45.0001, 24.0001, 'hamlet'),
                 _place_node('Estate', 45.0002, 24.0002, 'suburb'),
                 _place_node('Real Village', 45.002, 24.002, 'village')]
        self.assertEqual(
            overpass.nearest_place_node(nodes, 45.0, 24.0)[1], 'Real Village'
        )

    def test_english_name_is_preferred(self):
        node = _place_node('Cluj-Napoca', 45.0, 24.0, 'city')
        node['tags']['name:en'] = 'Cluj'
        self.assertEqual(
            overpass.nearest_place_node([node], 45.0, 24.0)[1], 'Cluj'
        )

    @patch('core.overpass._call')
    def test_the_query_intersects_every_probe_point(self, call):
        call.return_value = []
        overpass.fetch_place_nodes([Point(-1.31, 52.99, srid=4326),
                                    Point(-1.30, 52.99, srid=4326),
                                    Point(-1.29, 52.99, srid=4326)])
        q = call.call_args[0][0]
        # Chaining `around` onto a node set filters it rather than adding
        # to it, so this is one request and an intersection.
        self.assertEqual(q.count('around:'), 3)
        self.assertIn('node.n0(around:', q)
        self.assertIn('node.n1(around:', q)
        self.assertIn('.n2 out;', q)
        self.assertIn('city|town|village', q)

    def test_no_points_makes_no_request(self):
        self.assertEqual(overpass.fetch_place_nodes([]), [])


class LocalityRankOrderTests(TestCase):
    """Where the node sits against containment — the ordering that keeps
    the measured risk off the common path."""

    def test_a_settlement_boundary_outranks_a_node(self):
        from core.overpass import PLACE_BOUNDARY_RANK, PLACE_NODE_RANK
        self.assertGreater(PLACE_BOUNDARY_RANK, PLACE_NODE_RANK)

    def test_a_civil_parish_outranks_a_node(self):
        from core.overpass import PLACE_NODE_RANK
        rank, _ = overpass.locality_best([_parish('Ingham CP')])
        self.assertGreater(rank, PLACE_NODE_RANK)

    def test_a_node_outranks_an_admin_district(self):
        """The whole point: a district named after a river or a distant
        commune seat is what the node is there to beat."""
        from core.overpass import PLACE_NODE_RANK
        rank, name = overpass.locality_best(
            [_area('Erewash', 8, designation='non_metropolitan_district')]
        )
        self.assertEqual(name, 'Erewash')
        self.assertLess(rank, PLACE_NODE_RANK)


class NodeRungResolveTests(ApiTestCase):
    """End to end, including when the extra request is and isn't spent."""

    @classmethod
    def setUpTestData(cls):
        _admin_box('Nottinghamshire', 'United Kingdom',
                   -1.4, 52.8, -0.7, 53.5, country_iso='GB')

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('noderung', password='pw12345!')
        self.client.force_login(self.user)

    def _post(self, name, lng, lat):
        return self.client.post(
            reverse('core:resolve'),
            {'name': name, 'class': 'road', 'lngLat': [lng, lat], 'zoom': 15},
            content_type='application/json',
        ).json()['place']['slug']

    @patch('core.resolve.overpass.fetch_place_nodes')
    @patch('core.resolve.overpass.fetch_elements')
    def test_a_district_is_beaten_by_the_town_you_are_in(self, fetch, nodes):
        fetch.return_value = [_area('Nottinghamshire', 6),
                              _area('Bassetlaw', 8,
                                    designation='non_metropolitan_district')]
        nodes.return_value = [_place_node('Worksop', 53.302, -1.124, 'town')]
        _make_place(name='Church Street', slug='church-street')
        self.assertEqual(
            self._post('Church Street', -1.124, 53.302), 'church-street-worksop'
        )

    @patch('core.resolve.overpass.fetch_place_nodes')
    @patch('core.resolve.overpass.fetch_elements')
    def test_a_parish_wins_and_costs_no_extra_request(self, fetch, nodes):
        """Where containment already has a settlement boundary, the node
        lookup is neither needed nor paid for."""
        fetch.return_value = [_area('Nottinghamshire', 6),
                              _area('Bassetlaw', 8), _parish('Elkesley CP')]
        _make_place(name='Church Street', slug='church-street')
        self.assertEqual(
            self._post('Church Street', -0.99, 53.25), 'church-street-elkesley'
        )
        nodes.assert_not_called()

    @patch('core.resolve.overpass.fetch_place_nodes')
    @patch('core.resolve.overpass.fetch_elements')
    def test_nothing_nearby_falls_back_to_the_district(self, fetch, nodes):
        # Open country: no settlement node within range, so the rung
        # declines rather than reaching for a distant town.
        fetch.return_value = [_area('Nottinghamshire', 6),
                              _area('Bassetlaw', 8)]
        nodes.return_value = []
        _make_place(name='Church Lane', slug='church-lane')
        self.assertEqual(
            self._post('Church Lane', -0.99, 53.25), 'church-lane-bassetlaw'
        )

    @patch('core.resolve.overpass.fetch_place_nodes')
    @patch('core.resolve.overpass.fetch_elements')
    def test_a_failed_node_lookup_mints_nothing(self, fetch, nodes):
        """It used to mint `church-lane-bassetlaw` here, on the reasoning
        that a coarse qualifier beats a failed click. It does not: the
        district is often not where the street is, and it takes the slug
        that district's own High Street needs. A 503 is repeatable; a
        slug is not."""
        fetch.return_value = [_area('Bassetlaw', 8)]
        nodes.side_effect = overpass.OverpassError('429')
        _make_place(name='Church Lane', slug='church-lane')
        response = self.client.post(
            reverse('core:resolve'),
            {'name': 'Church Lane', 'class': 'road',
             'lngLat': [-0.99, 53.25], 'zoom': 15},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Place.objects.filter(slug__startswith='church-lane-')
                         .exists())

    @patch('core.resolve.overpass.fetch_place_nodes')
    @patch('core.resolve.overpass.fetch_elements')
    def test_a_settlement_boundary_survives_a_node_outage(self, fetch, nodes):
        """The rung that isn't reached cannot fail the mint: containment
        already had a parish, so no node lookup is made to go wrong."""
        fetch.return_value = [_area('Bassetlaw', 8), _parish('Elkesley CP')]
        nodes.side_effect = overpass.OverpassError('429')
        _make_place(name='Church Lane', slug='church-lane')
        self.assertEqual(
            self._post('Church Lane', -0.99, 53.25), 'church-lane-elkesley'
        )


class LocalityQualifierAuditTests(TestCase):
    """audit_locality_qualifiers: finding slugs the old swallowed
    OverpassError left naming the wrong place.

    The fixture is the real one — a High Street in Mexborough that took
    `high-street-doncaster` because the node rung was unreachable at mint
    time [dev, 2026-08-24]."""

    MEXBOROUGH = (-1.2898, 53.4934)

    @classmethod
    def setUpTestData(cls):
        _admin_box('Doncaster', 'United Kingdom', -1.4, 53.3, -0.9, 53.6)

    def _street(self, slug, osm_id=55304610):
        lon, lat = self.MEXBOROUGH
        return Place.objects.create(
            slug=slug,
            anchor_level=Place.AnchorLevel.OSM,
            display_name='High Street',
            feature_class='road',
            osm_type='way',
            osm_id=osm_id,
            centroid=Point(lon, lat, srid=4326),
            label_point=Point(lon, lat, srid=4326),
            # Short enough that _probe_set declines to probe, so the audit
            # asks for the click's areas once and nothing more.
            geometry=LineString(
                [(-1.2924, 53.4932), (-1.2879, 53.4938)], srid=4326
            ),
        )

    def _run(self, **opts):
        out = StringIO()
        call_command('audit_locality_qualifiers', interval=0, stdout=out,
                     **opts)
        return out.getvalue()

    @patch('core.overpass.fetch_place_nodes')
    @patch('core.overpass.fetch_common_areas')
    def test_reports_a_slug_the_rung_would_now_improve(self, areas, nodes):
        areas.return_value = [
            _area('Doncaster', 8, designation='metropolitan_district')
        ]
        nodes.return_value = [
            _place_node('Mexborough', 53.4937, -1.2910, 'town')
        ]
        self._street('high-street-doncaster')
        output = self._run()
        self.assertIn('high-street-doncaster', output)
        self.assertIn('would now be: high-street-mexborough', output)
        self.assertIn('1 degraded', output)

    @patch('core.overpass.fetch_place_nodes')
    @patch('core.overpass.fetch_common_areas')
    def test_a_slug_already_on_the_ladder_is_left_alone(self, areas, nodes):
        areas.return_value = [
            _area('Doncaster', 8, designation='metropolitan_district')
        ]
        nodes.return_value = [
            _place_node('Mexborough', 53.4937, -1.2910, 'town')
        ]
        self._street('high-street-mexborough')
        self.assertIn('0 degraded', self._run())

    @patch('core.overpass.fetch_place_nodes')
    @patch('core.overpass.fetch_common_areas')
    def test_a_taken_better_slug_is_blocked_not_degraded(self, areas, nodes):
        """Deleting this row would free nothing — another place holds the
        slug it would want — so it is a collision, not damage."""
        areas.return_value = [
            _area('Doncaster', 8, designation='metropolitan_district')
        ]
        nodes.return_value = [
            _place_node('Mexborough', 53.4937, -1.2910, 'town')
        ]
        self._street('high-street-mexborough', osm_id=99000001)
        self._street('high-street-doncaster')
        output = self._run()
        self.assertIn('0 degraded, 1 blocked', output)
        self.assertIn('held by high-street-mexborough', output)

    @patch('core.overpass.fetch_place_nodes')
    @patch('core.overpass.fetch_common_areas')
    def test_a_bare_slug_is_never_degraded(self, areas, nodes):
        """The incumbent keeps the bare slug by design (`SLUGS.md` §5), so
        it is skipped before a single Overpass call is spent on it."""
        self._street('high-street')
        self.assertIn('skipped 1', self._run(verbose_skips=True))
        areas.assert_not_called()
        nodes.assert_not_called()

    @patch('core.overpass.fetch_place_nodes')
    @patch('core.overpass.fetch_common_areas')
    def test_an_overpass_failure_is_reported_not_swallowed(self, areas, nodes):
        """The bug this audit exists to find was a swallowed failure, so
        the audit must not repeat it: an unaskable row is named, not
        counted as clean."""
        areas.side_effect = overpass.OverpassError('429')
        self._street('high-street-doncaster')
        output = self._run()
        self.assertIn('1 unchecked', output)
        self.assertIn('high-street-doncaster', output)


class SlugQualifierTests(TestCase):
    """unique_slug's ladder: bare, then qualified, then numeric."""

    def _mint(self, name, qualifier=None):
        from .slugs import unique_slug
        place = _make_place(name=name, slug=unique_slug(name, qualifier))
        return place.slug

    def test_first_place_keeps_the_bare_slug(self):
        self.assertEqual(self._mint('Portland', 'oregon'), 'portland')

    def test_second_place_takes_the_qualifier(self):
        self._mint('Portland', 'oregon')
        self.assertEqual(self._mint('Portland', 'maine'), 'portland-maine')

    def test_numeric_floor_when_there_is_no_qualifier(self):
        self._mint('Springfield')
        self.assertEqual(self._mint('Springfield'), 'springfield-2')

    def test_numeric_counts_on_the_qualified_form(self):
        # Two Portlands in the same Oregon: `portland-oregon-2` tells a
        # reader more than `portland-3` would.
        self._mint('Portland', 'oregon')
        self.assertEqual(self._mint('Portland', 'oregon'), 'portland-oregon')
        self.assertEqual(
            self._mint('Portland', 'oregon'), 'portland-oregon-2'
        )
        self.assertEqual(
            self._mint('Portland', 'oregon'), 'portland-oregon-3'
        )

    def test_qualifier_steps_over_a_parked_alias(self):
        first = self._mint('Portland', 'oregon')
        place = Place.objects.get(slug=first)
        PlaceSlug.objects.create(
            place=place, slug='portland-maine', is_canonical=False
        )
        self.assertEqual(
            self._mint('Portland', 'maine'), 'portland-maine-2'
        )

    def test_long_name_plus_qualifier_fits_the_column(self):
        # 100-char base + '-' + 45-char qualifier = 146, inside the 150
        # the slug columns were widened to. A DataError here means the
        # migration and the budget have drifted apart.
        name = 'Llanfair' * 25
        qualifier = 'a' * 45
        self._mint(name)
        slug = self._mint(name, qualifier)
        self.assertEqual(slug, f'{slugify(name)[:100]}-{qualifier}')
        self.assertLessEqual(len(slug), 150)
        self.assertEqual(Place.objects.get(slug=slug).slug, slug)


class SlugTransliterationTests(TestCase):
    """Letters that are letters, not accented bases: slugify decomposes and
    drops them, so they get spelled out before it runs (Tromsø minted the
    slug `troms` until this landed)."""

    def test_undecomposable_letters_survive(self):
        from .slugs import unique_slug
        self.assertEqual(unique_slug('Tromsø'), 'tromso')
        self.assertEqual(unique_slug('Łódź'), 'lodz')
        self.assertEqual(unique_slug('Ærø'), 'aero')
        self.assertEqual(unique_slug('Þingvellir'), 'thingvellir')

    def test_dotless_i_is_an_i(self):
        # Kırşehir has a name:en and it is byte-identical to the native
        # name, so no English-first ladder upstream would have caught this.
        from .slugs import unique_slug
        self.assertEqual(unique_slug('Kırşehir'), 'kirsehir')

    def test_one_letter_can_become_two(self):
        from .slugs import unique_slug
        self.assertEqual(unique_slug('Straße'), 'strasse')

    def test_accented_bases_are_untouched(self):
        from .slugs import unique_slug
        self.assertEqual(unique_slug('Kraków'), 'krakow')
        self.assertEqual(unique_slug('Bạc Liêu'), 'bac-lieu')


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


class FeatureClassAllowlistTests(ApiTestCase):
    """The server-side half of the POI rule.

    `web/src/poi.ts` keeps commercial categories off the map and out of
    search, but `feature_class` is a string the client picks — so until this
    existed, a hand-written POST could still mint the restaurant that takes
    the town's slug.
    """

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('drew', password='pw12345!')

    def _post(self, **overrides):
        payload = {
            'name': 'Ojai',
            'class': 'restaurant',
            'lngLat': [14.44, 50.08],
            'zoom': 16,
        }
        payload.update(overrides)
        return self.client.post(
            reverse('core:resolve'), payload, content_type='application/json'
        )

    @patch('core.resolve.overpass.fetch_elements')
    def test_commercial_class_is_refused(self, fetch):
        fetch.return_value = [_relation()]
        self.client.force_login(self.user)
        response = self._post()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['reason'], 'disallowed_class')
        # Named, so a wrongly-rejected category produces a usable report.
        self.assertIn('restaurant', response.json()['error'])
        fetch.assert_not_called()
        self.assertEqual(Place.objects.count(), 0)

    @patch('core.resolve.overpass.fetch_elements')
    def test_refusal_precedes_the_signin_wall(self, fetch):
        """A bad class is a bad request whoever sends it. Answering 401 here
        would tell an anonymous caller to sign in for a request that was
        never going to work."""
        fetch.return_value = [_relation()]
        response = self._post()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['reason'], 'disallowed_class')
        fetch.assert_not_called()

    @patch('core.resolve.overpass.fetch_elements')
    def test_toponymic_class_still_resolves(self, fetch):
        fetch.return_value = [_relation()]
        self.client.force_login(self.user)
        response = self._post(name='Mississippi River', **{'class': 'waterway'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['created'])

    @patch('core.resolve.overpass.fetch_elements')
    def test_existing_place_resolves_despite_its_class(self, fetch):
        """Enforcement is on creation only. Retiring a category must not
        strand articles already written under it — and the dev database has
        rows predating this list."""
        fetch.return_value = [_relation()]
        Place.objects.create(
            display_name='Ojai',
            slug='ojai',
            feature_class='restaurant',
            centroid=Point(14.44, 50.08, srid=4326),
            anchor_level=3,
        )
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['created'])
        self.assertEqual(response.json()['place']['slug'], 'ojai')
        fetch.assert_not_called()

    def test_client_fly_zoom_classes_are_all_allowed(self):
        """`FLY_ZOOM_BY_CLASS` in MapView.tsx names classes the client expects
        to frame after a resolve. Any of them missing from the allowlist is a
        contradiction between the two halves — the client would be planning a
        camera move for a place the server refuses to create.

        Skipped when the frontend isn't beside the server: the deployed app
        ships `web/dist`, not `web/src`.
        """
        source = (
            Path(settings.BASE_DIR).parent / 'web' / 'src' / 'map'
            / 'MapView.tsx'
        )
        if not source.exists():
            self.skipTest('web/src not present next to the server')
        text = source.read_text('utf-8')
        block = re.search(
            r'FLY_ZOOM_BY_CLASS: Record<string, number> = \{(.*?)\}',
            text,
            re.S,
        )
        self.assertIsNotNone(block, 'FLY_ZOOM_BY_CLASS moved or was renamed')
        classes = re.findall(r'^\s*(\w+):', block.group(1), re.M)
        self.assertTrue(classes)
        missing = sorted(set(classes) - ALLOWED_FEATURE_CLASSES)
        self.assertEqual(missing, [])

    @patch('core.resolve.overpass.fetch_elements')
    def test_the_poi_source_layer_name_is_not_a_class(self, fetch):
        """A map click used to report the `poi` source layer rather than the
        POI's own class, so 'poi' had to be permitted wholesale and told the
        server nothing about what was being created. `kindOf()` now sends the
        class; nothing should ever again post the layer's name."""
        self.client.force_login(self.user)
        response = self._post(**{'class': 'poi'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['reason'], 'disallowed_class')
        fetch.assert_not_called()
        self.assertEqual(Place.objects.count(), 0)

    def test_client_poi_allowlist_classes_are_all_allowed(self):
        """`POI_CLASS_ALLOWLIST` in poi.ts is exactly what the map lets you
        click on the `poi` layer, and `kindOf()` now forwards those classes
        verbatim. One missing here is a category the map invites you to click
        and the server then refuses.

        Skipped when the frontend isn't beside the server: the deployed app
        ships `web/dist`, not `web/src`.
        """
        source = Path(settings.BASE_DIR).parent / 'web' / 'src' / 'poi.ts'
        if not source.exists():
            self.skipTest('web/src not present next to the server')
        text = source.read_text('utf-8')
        block = re.search(r'POI_CLASS_ALLOWLIST = \[(.*?)\]', text, re.S)
        self.assertIsNotNone(
            block, 'POI_CLASS_ALLOWLIST moved or was renamed'
        )
        classes = re.findall(r"'([^']+)'", block.group(1))
        self.assertTrue(classes)
        self.assertEqual(sorted(set(classes) - ALLOWED_FEATURE_CLASSES), [])
        # Stations are the fourth clickable POI class and the one exception:
        # `kindOf()` renames the tile schema's `railway` to `station`, the
        # word `kindFromPhoton()` already produces for the same feature.
        self.assertIn('station', ALLOWED_FEATURE_CLASSES)
        self.assertNotIn('railway', ALLOWED_FEATURE_CLASSES)

    def test_seed_command_classes_are_all_allowed(self):
        """The demo seeder posts the same shape a map click does, so a class
        it uses is one a real click can produce."""
        from .management.commands.seed_demo_content import TARGETS

        used = {target[1] for target in TARGETS.values()}
        self.assertEqual(sorted(used - ALLOWED_FEATURE_CLASSES), [])


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
        # Case-insensitive so a tag the shell emitted as <SCRIPT> would fail
        # this test rather than quietly fall out of the sample it inspects.
        tags = re.findall(r'<script[^>]*>', html, re.IGNORECASE)
        src_tags = [t for t in tags if ' src=' in t]
        self.assertTrue(src_tags)
        for tag in src_tags:
            self.assertNotIn('nonce=', tag)

    def test_index_html_redirects_instead_of_being_served_raw(self):
        """WhiteNoise's root is the Vite build, so without the exclusion in
        core/statics.py it would serve index.html off disk — bypassing the
        nonce stamping and the SEO meta, silently, on a URL that looks fine."""
        response = self.client.get('/index.html')
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response.headers['Location'], '/')

    def test_nonce_is_per_request(self):
        first = self._csp(self.client.get('/'))['script-src']
        second = self._csp(self.client.get('/'))['script-src']
        self.assertNotEqual(first, second)

    def test_no_nonce_is_minted_for_responses_without_inline_scripts(self):
        """LazyNonce only generates on access, so an API response shouldn't
        carry one. Guards the laziness, not just the header."""
        response = self.client.get('/api/highlights/?bbox=0,0,1,1')
        self.assertEqual(
            self._csp(response)['script-src'],
            "script-src 'self' 'wasm-unsafe-eval'",
        )

    def test_webassembly_is_allowed_for_the_rtl_shaper(self):
        """MapLibre's RTL text plugin is WebAssembly, and script-src governs
        WASM compilation. Drop this and Arabic and Hebrew labels vanish —
        with a console error and nothing else, which is why it's pinned."""
        csp = self._csp(self.client.get('/'))
        self.assertIn("'wasm-unsafe-eval'", csp['script-src'])

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
        # Quoted, because 'wasm-unsafe-eval' is allowed and contains this as
        # a substring. The two are not the same permission: one compiles
        # WebAssembly, the other runs arbitrary source.
        self.assertNotIn("'unsafe-eval'", csp['script-src'])


@override_settings(ALLOWED_HOSTS=['testserver'])
class CspReportingTests(ApiTestCase):
    """The endpoint that makes a broken policy visible.

    A CSP violation is blocked in the browser and reported nowhere else, so
    without this the only symptom is a page that renders subtly wrong for
    someone you never hear from. Every test here is about that silence: the
    two wire formats, the noise filter that keeps the log worth reading, and
    the path/group names that would report into a void if they drifted.
    """

    URI_REPORT = {
        'csp-report': {
            'document-uri': 'https://toponymia.org/place/paris',
            'effective-directive': 'script-src',
            'blocked-uri': 'https://evil.example/x.js',
            'source-file': 'https://toponymia.org/assets/index.js',
            'line-number': 42,
        }
    }
    API_REPORT = [
        {
            'type': 'csp-violation',
            'url': 'https://toponymia.org/place/paris',
            'body': {
                'effectiveDirective': 'img-src',
                'blockedURL': 'https://evil.example/pixel.png',
                'sourceFile': 'https://toponymia.org/assets/index.js',
                'lineNumber': 7,
            },
        }
    ]

    def _post(self, body, content_type='application/csp-report', **kwargs):
        return self.client.post(
            reverse('core:csp-report'),
            json.dumps(body),
            content_type=content_type,
            **kwargs,
        )

    def _capture(self):
        """Swap the *configured* handler's stream, as ErrorLoggingTests does.

        assertLogs would attach a handler of its own and so pass even if the
        LOGGING block were deleted — which is the failure this endpoint is
        supposed to make impossible.
        """
        handlers = logging.getLogger('core').handlers
        stream_handlers = [
            h for h in handlers if isinstance(h, logging.StreamHandler)
        ]
        self.assertTrue(stream_handlers, 'core has no stream handler')
        return stream_handlers[0]

    @contextmanager
    def _logged(self):
        handler = self._capture()
        captured = StringIO()
        original, handler.stream = handler.stream, captured
        try:
            yield captured
        finally:
            handler.stream = original

    def test_report_uri_format_is_logged(self):
        with self._logged() as log:
            response = self._post(self.URI_REPORT)
        self.assertEqual(response.status_code, 204)
        written = log.getvalue()
        self.assertIn('script-src', written)
        self.assertIn('https://evil.example/x.js', written)
        self.assertIn('/place/paris', written)

    def test_reporting_api_format_is_logged(self):
        """The camelCase, batched, `application/reports+json` shape Chrome
        sends. Handling only the deprecated format would lose these with no
        sign that anything was missing."""
        with self._logged() as log:
            response = self._post(
                self.API_REPORT, content_type='application/reports+json'
            )
        self.assertEqual(response.status_code, 204)
        written = log.getvalue()
        self.assertIn('img-src', written)
        self.assertIn('https://evil.example/pixel.png', written)

    def test_extension_violations_are_dropped(self):
        """Browser extensions inject scripts into every page and their
        blocked loads are reported as violations of our policy. This is the
        usual reason CSP reporting gets switched off as noise."""
        body = {'csp-report': dict(self.URI_REPORT['csp-report'])}
        body['csp-report']['blocked-uri'] = 'chrome-extension://abcd/inject.js'
        with self._logged() as log:
            response = self._post(body)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(log.getvalue(), '')

    def test_an_unrecognised_body_is_accepted_and_ignored(self):
        """The browser discards the response, so there is nobody to tell.
        Answering 400 would only mean a retry loop."""
        with self._logged() as log:
            response = self._post({'not': 'a report'})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(log.getvalue(), '')

    def test_a_batch_is_capped(self):
        """`report-to` posts an array whose length the client picks."""
        with self._logged() as log:
            response = self._post(
                self.API_REPORT * 50,
                content_type='application/reports+json',
            )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(log.getvalue().count('CSP violation'), MAX_REPORTS)

    def test_long_fields_are_clipped(self):
        """A blocked `data:` URL can be megabytes, and this is a write path
        anyone can reach — so what it can put in the log is bounded."""
        body = {'csp-report': dict(self.URI_REPORT['csp-report'])}
        body['csp-report']['blocked-uri'] = 'data:image/png;base64,' + 'A' * 5000
        with self._logged() as log:
            self._post(body)
        self.assertLess(len(log.getvalue()), 1000)

    def test_no_authentication_or_csrf_token_is_needed(self):
        """The browser posts these with no credentials and no token. DRF's
        SessionAuthentication would demand CSRF of a session cookie that rode
        along on the same-origin post, which is why the view declares no
        authentication at all."""
        self.client = Client(enforce_csrf_checks=True)
        response = self._post(self.URI_REPORT)
        self.assertEqual(response.status_code, 204)

    def test_get_is_not_allowed(self):
        response = self.client.get(reverse('core:csp-report'))
        self.assertEqual(response.status_code, 405)

    def test_reports_are_throttled(self):
        """The only write path on the site reachable without an account."""
        limit = int(
            settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'][
                'csp-report'
            ].split('/')[0]
        )
        # Captured only to keep 30 real log lines out of the suite's output.
        with self._logged():
            for _ in range(limit):
                self.assertEqual(self._post(self.URI_REPORT).status_code, 204)
            self.assertEqual(self._post(self.URI_REPORT).status_code, 429)

    def test_the_policy_reports_to_the_route_that_exists(self):
        """`SECURE_CSP` spells the path as a literal because the policy is
        built before the URLConf loads. A rename on either side would send
        every report into a 404 that nothing would ever notice."""
        self.assertEqual(
            settings.SECURE_CSP['report-uri'],
            [reverse(REPORT_ROUTE)],
        )

    def test_report_to_is_not_declared(self):
        """Pinning a measurement, not a preference.

        Adding `report-to` beside `report-uri` reads as the forwards-compatible
        move and is the reverse: in Chromium 151 it makes Chrome ignore
        `report-uri` and then deliver nothing at all, over HTTP and over real
        HTTPS alike. Since CSP reporting fails silently, turning it off that
        way would look exactly like a policy with no violations. The reasoning
        is in settings.SECURE_CSP; this is the tripwire.
        """
        self.assertNotIn('report-to', settings.SECURE_CSP)
        response = self.client.get('/')
        self.assertNotIn('report-to', response.headers['Content-Security-Policy'])


@override_settings(ALLOWED_HOSTS=['testserver'])
class ErrorLoggingTests(TestCase):
    """That an unhandled 500 leaves a trace on the server.

    Django's own defaults lose exactly this: DEFAULT_LOGGING filters the
    console handler on require_debug_true and mail_admins on
    require_debug_false, so with DEBUG off and no ADMINS an unhandled
    exception is reported nowhere. Nothing about that failure is visible from
    the outside — the user gets the same generic 500 either way — which is why
    it needs a test rather than a look at the log.
    """

    def _raise(self, *args, **kwargs):
        raise RuntimeError('boom')

    def _configured_handler(self):
        """The live stderr handler off the configured logger.

        Asserting through this rather than through assertLogs is the point:
        assertLogs attaches a handler of its own, so it would report success
        even with the whole LOGGING block deleted — the exact shape of
        false pass this config exists to prevent.
        """
        handlers = logging.getLogger('django.request').handlers
        stream_handlers = [
            h for h in handlers if isinstance(h, logging.StreamHandler)
        ]
        self.assertTrue(stream_handlers, 'django.request has no stream handler')
        return stream_handlers[0]

    @override_settings(DEBUG=False)
    def test_unhandled_exception_reaches_the_real_handler_with_debug_off(self):
        handler = self._configured_handler()
        captured = StringIO()
        original, handler.stream = handler.stream, captured
        # raise_request_exception=False lets the 500 be returned rather than
        # re-raised into the test, so this exercises the path a real request
        # takes — which is the path that does the logging.
        client = Client(raise_request_exception=False)
        try:
            with patch('core.views.published_q', self._raise):
                response = client.get('/api/highlights/?bbox=0,0,1,1')
        finally:
            handler.stream = original
        self.assertEqual(response.status_code, 500)
        written = captured.getvalue()
        self.assertIn('RuntimeError', written)
        self.assertIn('boom', written)
        # The traceback, not just the one-line message: without exc_info a
        # report tells you a 500 happened but not where.
        self.assertIn('Traceback', written)

    def test_handler_carries_no_debug_filter(self):
        """The single line that makes the above work. A require_debug_true
        filter here would pass every test that runs with DEBUG on and report
        nothing in production."""
        self.assertEqual(self._configured_handler().filters, [])

    def test_request_errors_do_not_propagate(self):
        """Otherwise the record also reaches the inherited 'django' logger and
        its mail_admins handler, double-reporting once ADMINS is set."""
        self.assertFalse(settings.LOGGING['loggers']['django.request']['propagate'])

    def test_disallowed_host_stays_quiet(self):
        """disable_existing_loggers=False is load-bearing: it keeps Django's
        null handler for DisallowedHost, without which bots probing Host
        headers bury real exceptions."""
        self.assertFalse(settings.LOGGING['disable_existing_loggers'])


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

    def test_privacy_url_serves_the_app(self):
        response = self.client.get('/privacy')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Privacy Policy', response.content.decode())

    def test_privacy_is_listed_in_the_sitemap(self):
        response = self.client.get(reverse('sitemap'))
        body = b''.join(response.streaming_content).decode()
        self.assertIn('/privacy', body)


class AccountManagementTests(ApiTestCase):
    """The self-serve account panel: password change, email change, closure."""

    PASSWORD = 'sturdy-passphrase-9'

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            'alice', password=self.PASSWORD, email='alice@example.com'
        )
        EmailAddress.objects.create(
            user=self.user, email='alice@example.com',
            verified=True, primary=True,
        )

    # --- password -------------------------------------------------------

    def test_password_change_requires_current_password(self):
        self.client.force_login(self.user)
        response = self.client.post(
            '/_allauth/browser/v1/account/password/change',
            {'current_password': 'wrong', 'new_password': 'another-good-one-4'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.PASSWORD))

    def test_password_change_succeeds(self):
        self.client.force_login(self.user)
        response = self.client.post(
            '/_allauth/browser/v1/account/password/change',
            {
                'current_password': self.PASSWORD,
                'new_password': 'another-good-one-4',
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('another-good-one-4'))

    # --- email ----------------------------------------------------------

    def test_email_change_is_two_step_and_replaces_the_old_address(self):
        # Verification is by code, so POSTing the address only *sends* one —
        # nothing is stored until the code comes back. The UI has to collect
        # it, exactly as signup does.
        self.client.force_login(self.user)
        response = self.client.post(
            '/_allauth/browser/v1/account/email',
            {'email': 'alice2@example.com'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(
            EmailAddress.objects.filter(email='alice2@example.com').exists()
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
        # ACCOUNT_CHANGE_EMAIL: the new address supersedes rather than joins.
        addresses = set(
            EmailAddress.objects.filter(user=self.user).values_list(
                'email', flat=True
            )
        )
        self.assertEqual(addresses, {'alice2@example.com'})

    def test_email_change_mail_does_not_borrow_the_signup_wording(self):
        """The two confirmation mails are separate templates, and must stay so.

        allauth ships `email_confirmation_signup_message.txt` as a bare include
        of `email_confirmation_message.txt`, so upstream sends one body for
        both. Anything written for the signup reader — who has no account yet —
        is wrong for the change reader, who has one and is moving its address:
        "Someone used this email address to create an account" reads as a
        security incident to someone who created nothing. Nothing about the
        wiring announces the sharing, so a future edit to one template is a
        silent edit to the other unless this test fails.
        """
        self.client.force_login(self.user)
        self.client.post(
            '/_allauth/browser/v1/account/email',
            {'email': 'alice2@example.com'},
            content_type='application/json',
        )
        body = mail.outbox[0].body
        self.assertNotIn('create an account', body)
        self.assertIn('new email address', body)
        # The mail goes to the *new* address, so a reader who didn't ask for
        # the change has no account here — anything describing the account's
        # state is about a stranger's account. The ignore line stays bare.
        self.assertNotIn('the account', body.split("wasn't you")[1])

        # And the signup mail keeps saying the thing that is true only there.
        mail.outbox.clear()
        self.client.logout()
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
        self.assertIn('create an account', mail.outbox[0].body)

    def test_a_completed_email_change_warns_the_old_address(self):
        """The account-takeover tripwire, and the reason
        ACCOUNT_EMAIL_NOTIFICATIONS is set at all.

        allauth's notification mails are gated behind that setting and
        `send_notification_mail` returns silently when it is off, which is the
        default — so this whole family failed by sending nothing, and nobody
        notices a mail that never arrives. That is the failure this test
        exists to catch, not the wording.

        The recipient is the point: `email_changed` goes to the address being
        moved *away from*, so it reaches the owner after someone else has taken
        the account and before the change has cost them access. A version of
        this mail sent to the new address would be addressed to the attacker.
        """
        self.client.force_login(self.user)
        self.client.post(
            '/_allauth/browser/v1/account/email',
            {'email': 'alice2@example.com'},
            content_type='application/json',
        )
        code = re.search(
            r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b', mail.outbox[0].body
        ).group(1)
        mail.outbox.clear()
        self.client.post(
            '/_allauth/browser/v1/auth/email/verify',
            {'key': code},
            content_type='application/json',
        )
        notice = next(m for m in mail.outbox if 'Email Changed' in m.subject)
        self.assertEqual(notice.to, [self.user.email])
        self.assertIn('alice2@example.com', notice.body)
        # Names a route that still works for someone locked out, and does not
        # recommend the one that doesn't — see base_notification.txt.
        self.assertIn('support@toponymia.org', notice.body)
        # Stock's evidence block is trimmed to the timestamp. Losing the
        # override would silently restore the other two, and mailing someone
        # their own IP is the kind of change that should be chosen, not
        # inherited.
        self.assertNotIn('IP address', notice.body)
        self.assertNotIn('Browser', notice.body)
        # The time is the field a reader checks against their own memory, so
        # it has to name its zone: unlabelled, it reads as local time to
        # everyone not on UTC and can look hours wrong.
        self.assertRegex(notice.body, r'- Time: \d\d? \w{3} \d{4}, \d\d:\d\d UTC')

    def test_a_password_change_notifies_the_account(self):
        self.client.force_login(self.user)
        mail.outbox.clear()
        self.client.post(
            '/_allauth/browser/v1/account/password/change',
            {
                'current_password': self.PASSWORD,
                'new_password': 'another-good-one-4',
            },
            content_type='application/json',
        )
        notice = next(m for m in mail.outbox if 'Password Changed' in m.subject)
        self.assertEqual(notice.to, [self.user.email])

    # --- closure --------------------------------------------------------

    def _close(self, password=PASSWORD):
        return self.client.post(
            reverse('core:account-close'),
            {'password': password},
            content_type='application/json',
        )

    def test_close_requires_the_password(self):
        self.client.force_login(self.user)
        self.assertEqual(self._close(password='nope').status_code, 400)
        self.assertTrue(User.objects.filter(username='alice').exists())

    def test_close_deletes_an_account_with_no_contributions(self):
        self.client.force_login(self.user)
        response = self._close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['outcome'], 'deleted')
        self.assertFalse(User.objects.filter(username='alice').exists())
        # The session is gone with it.
        self.assertIsNone(self.client.get(reverse('core:me')).json()['user'])

    def test_close_anonymizes_a_contributor(self):
        place = Place.objects.create(
            slug='anonplace', anchor_level='name', display_name='Anonplace',
            feature_class='city', centroid=Point(0, 0),
        )
        save_edit(
            place, self.user,
            {'names': [{'name': 'Anonplace', 'language': 'eng',
                        'is_endonym': False,
                        'etymologies': [{'etymology_md': 'x'}]}],
             'body_md': '', 'derivations': [], 'see_also': []},
            'first',
        )
        self.client.force_login(self.user)
        response = self._close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['outcome'], 'anonymized')

        self.user.refresh_from_db()
        # Identity gone...
        self.assertTrue(self.user.username.startswith('[deleted-'))
        self.assertEqual(self.user.email, '')
        self.assertFalse(self.user.is_active)
        self.assertFalse(self.user.has_usable_password())
        self.assertFalse(
            EmailAddress.objects.filter(user=self.user).exists()
        )
        # ...contribution kept, still attributed to the same row.
        revision = Revision.objects.get(article__place=place)
        self.assertEqual(revision.author_id, self.user.id)

    def test_close_is_refused_while_banned(self):
        Ban.objects.create(user=self.user, reason='spam')
        self.client.force_login(self.user)
        response = self._close()
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(username='alice').exists())

    def test_sentinel_username_cannot_be_registered(self):
        # Square brackets fail username validation, so a closed account's
        # name can never be claimed or impersonated.
        from django.core.exceptions import ValidationError

        from .validators import username_validators

        with self.assertRaises(ValidationError):
            for validator in username_validators:
                validator('[deleted-abc123]')


class ClosedAccountReuseTests(ApiTestCase):
    """What a closed account frees up, and what it doesn't. Closing is the
    de-facto way to start over, so the email address has to be usable again —
    unless the account was banned, where BannedEmail holds. The *username*
    goes the other way: it is retired permanently, because the archive that
    carries it stays public."""

    PASSWORD = 'sturdy-passphrase-9'
    SIGNUP = '/_allauth/browser/v1/auth/signup'

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            'alice', password=self.PASSWORD, email='alice@example.com'
        )
        EmailAddress.objects.create(
            user=self.user, email='alice@example.com',
            verified=True, primary=True,
        )
        # Give her history, so closing anonymizes rather than deletes.
        place = Place.objects.create(
            slug='reuseplace', anchor_level='name', display_name='Reuseplace',
            feature_class='city', centroid=Point(0, 0),
        )
        save_edit(
            place, self.user,
            {'names': [{'name': 'Reuseplace', 'language': 'eng',
                        'is_endonym': False,
                        'etymologies': [{'etymology_md': 'x'}]}],
             'body_md': '', 'derivations': [], 'see_also': []},
            'first',
        )

    def _close(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('core:account-close'),
            {'password': self.PASSWORD},
            content_type='application/json',
        )
        self.client.logout()
        return response

    def _signup(self, username, email):
        return self.client.post(
            self.SIGNUP,
            {
                'username': username,
                'email': email,
                'password': self.PASSWORD,
                'terms': True,
            },
            content_type='application/json',
        )

    def test_email_is_reusable_after_closing(self):
        self.assertEqual(self._close().json()['outcome'], 'anonymized')
        # 401 = created, pending verification.
        self.assertEqual(
            self._signup('alice2', 'alice@example.com').status_code, 401
        )
        self.assertTrue(User.objects.filter(username='alice2').exists())

    def test_old_username_is_retired_by_closing(self):
        self._close()
        self.assertEqual(
            self._signup('alice', 'someone@example.com').status_code, 400
        )
        # Nobody holds the name: not a new account, and not the closed row,
        # which kept only its sentinel.
        self.assertFalse(User.objects.filter(username='alice').exists())
        revision = Revision.objects.get(article__place__slug='reuseplace')
        self.assertEqual(revision.author_id, self.user.id)
        self.assertTrue(revision.author.username.startswith('[deleted-'))

    def test_retired_username_is_matched_case_insensitively(self):
        # Otherwise "Alice" walks straight past a reservation on "alice".
        self._close()
        self.assertEqual(
            self._signup('ALICE', 'someone@example.com').status_code, 400
        )

    def test_reservation_keeps_no_link_to_the_closed_account(self):
        # A foreign key here would map [deleted-…] back to "alice" for anyone
        # with database or admin access, undoing the anonymization.
        self._close()
        row = ReservedUsername.objects.get(username='alice')
        self.assertEqual(
            {field.name for field in row._meta.get_fields()},
            {'id', 'username', 'created', 'expires'},
        )

    def test_closing_without_contributions_does_not_retire_the_name(self):
        # Nothing points at the row, so it is deleted outright — no history
        # left to misattribute, and so no reason to hold the name.
        bob = User.objects.create_user(
            'bob', password=self.PASSWORD, email='bob@example.com'
        )
        self.client.force_login(bob)
        self.client.post(
            reverse('core:account-close'),
            {'password': self.PASSWORD},
            content_type='application/json',
        )
        self.client.logout()
        self.assertFalse(ReservedUsername.objects.filter(username='bob'))
        self.assertEqual(
            self._signup('bob', 'bob2@example.com').status_code, 401
        )

    def test_expired_reservation_releases_the_name(self):
        # Reservations are permanent today, but the expiry is honoured so the
        # policy can be loosened later without touching the signup path.
        self._close()
        ReservedUsername.objects.filter(username='alice').update(
            expires=timezone.now() - timedelta(days=1)
        )
        self.assertEqual(
            self._signup('alice', 'someone@example.com').status_code, 401
        )

    def test_banned_account_email_stays_blocked(self):
        # Closing is refused while banned, so the only route here is a ban
        # that outlives the account — which is BannedEmail's whole job.
        from .moderation import block_user_emails

        block_user_emails(self.user, None, reason='spam')
        self.assertEqual(
            self._signup('alice2', 'alice@example.com').status_code, 400
        )
