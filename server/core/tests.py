from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import LineString, Point
from django.test import TestCase
from django.urls import reverse

from .models import Article, Place, PlaceName, Revision
from .overpass import (
    OverpassError,
    center_of,
    choose_element,
    qid_of,
    radius_for_click,
)


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


class ResolveApiTests(TestCase):
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

    @patch('core.resolve.overpass.fetch_elements')
    def test_same_qid_from_distant_click_reuses_place(self, fetch):
        fetch.return_value = [_relation()]
        first = self._post().json()
        # 1400 km downstream: outside any proximity cache, same QID.
        second = self._post(lngLat=[-89.0, 44.5]).json()
        self.assertFalse(second['created'])
        self.assertEqual(second['place']['id'], first['place']['id'])
        self.assertEqual(Place.objects.count(), 1)

    @patch('core.resolve.overpass.fetch_elements')
    def test_nearby_click_hits_cache_without_overpass(self, fetch):
        fetch.return_value = [_relation()]
        first = self._post().json()
        fetch.reset_mock()
        second = self._post(lngLat=[-91.0005, 32.0005]).json()
        fetch.assert_not_called()
        self.assertFalse(second['created'])
        self.assertEqual(second['place']['id'], first['place']['id'])

    @patch('core.resolve.overpass.fetch_way_geometry')
    @patch('core.resolve.overpass.fetch_elements')
    def test_osm_anchor_when_no_qid(self, fetch, fetch_geom):
        fetch.return_value = [_way()]
        fetch_geom.return_value = [(-122.6, 45.0), (-122.4, 45.2)]
        place = self._post(
            name='Mill Creek', lngLat=[-122.5, 45.1]
        ).json()['place']
        self.assertEqual(place['anchor_level'], 'osm')
        self.assertIsNone(place['wikidata_qid'])
        self.assertEqual(place['osm_type'], 'way')
        db_place = Place.objects.get(pk=place['id'])
        self.assertEqual(db_place.geometry.geom_type, 'LineString')

    @patch('core.resolve.overpass.fetch_elements')
    def test_name_anchor_when_overpass_empty(self, fetch):
        fetch.return_value = []
        body = self._post(name='Vanished Hamlet', **{'class': 'city'}).json()
        place = body['place']
        self.assertEqual(place['anchor_level'], 'name')
        self.assertIsNone(place['osm_id'])
        self.assertEqual(place['centroid'], [-91.0, 32.0])

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


class ArticleApiTests(TestCase):
    def setUp(self):
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

    def test_detail_returns_current_article(self):
        self.client.force_login(self.user)
        self._put()
        self.client.logout()
        body = self.client.get(
            reverse('core:place-detail', args=[self.place.slug])
        ).json()
        self.assertEqual(body['article']['author'], 'drew')
        self.assertIn('Founded', body['article']['content']['body_md'])


def _publish(place, user):
    """Give a place a current revision, making it a highlight candidate."""
    article = Article.objects.create(place=place)
    revision = Revision.objects.create(
        article=article, author=user, comment='', content=_content()
    )
    article.current_revision = revision
    article.save(update_fields=['current_revision'])


class HighlightApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('drew', password='pw12345!')

    def _get(self, bbox='9,49,11,51'):
        return self.client.get(
            reverse('core:highlights'), {'bbox': bbox}
        )

    def test_rejects_malformed_bbox(self):
        for bbox in ('', '1,2,3', '1,2,3,x'):
            self.assertEqual(self._get(bbox).status_code, 400)

    def test_place_without_article_excluded(self):
        _make_place()
        body = self._get().json()
        self.assertEqual(body['type'], 'FeatureCollection')
        self.assertEqual(body['features'], [])

    def test_centroid_fallback_point_feature(self):
        _publish(_make_place(), self.user)  # centroid (10, 50), no geometry
        features = self._get().json()['features']
        self.assertEqual(len(features), 1)
        feature = features[0]
        self.assertEqual(feature['geometry']['type'], 'Point')
        self.assertEqual(feature['geometry']['coordinates'], [10.0, 50.0])
        self.assertEqual(feature['properties']['slug'], 'testville')
        self.assertEqual(feature['properties']['display_name'], 'Testville')

    def test_line_geometry_returned_and_bbox_filtered(self):
        river = Place.objects.create(
            slug='test-river',
            anchor_level=Place.AnchorLevel.OSM,
            osm_type='way',
            osm_id=99,
            display_name='Test River',
            feature_class='waterway',
            geometry=LineString([(9.5, 50.2), (10.5, 50.4)], srid=4326),
            centroid=Point(10.0, 50.3, srid=4326),
        )
        _publish(river, self.user)
        features = self._get().json()['features']
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]['geometry']['type'], 'LineString')
        # viewport far away: nothing
        self.assertEqual(self._get('-20,-10,-18,-8').json()['features'], [])

    def test_wrapped_viewport_falls_back_to_world(self):
        _publish(_make_place(), self.user)
        features = self._get('150,-60,210,60').json()['features']
        self.assertEqual(len(features), 1)


class RelationGeometryTests(TestCase):
    """First article save backfills simplified relation geometry."""

    def setUp(self):
        self.user = User.objects.create_user('drew', password='pw12345!')
        self.place = Place.objects.create(
            slug='test-river',
            anchor_level=Place.AnchorLevel.WIKIDATA,
            wikidata_qid='Q999999',
            osm_type='relation',
            osm_id=555,
            display_name='Test River',
            feature_class='waterway',
            centroid=Point(10.0, 50.0, srid=4326),
        )
        self.client.force_login(self.user)

    def _put(self):
        return self.client.put(
            reverse('core:article-edit', args=[self.place.slug]),
            {'content': _content(), 'comment': 'draft'},
            content_type='application/json',
        )

    @patch('core.articles.overpass.fetch_relation_geometry')
    def test_save_fetches_and_simplifies_relation_geometry(self, fetch):
        fetch.return_value = [
            [(10.0, 50.0), (10.00001, 50.00001), (10.1, 50.1)],
            [(10.1, 50.1), (10.2, 50.2)],
        ]
        self.assertEqual(self._put().status_code, 200)
        fetch.assert_called_once_with(555)
        self.place.refresh_from_db()
        self.assertEqual(self.place.geometry.geom_type, 'MultiLineString')
        # the near-duplicate vertex is simplified away
        self.assertEqual(len(self.place.geometry[0]), 2)

    @patch('core.articles.overpass.fetch_relation_geometry')
    def test_second_save_does_not_refetch(self, fetch):
        fetch.return_value = [[(10.0, 50.0), (10.1, 50.1)]]
        self._put()
        fetch.reset_mock()
        self.assertEqual(self._put().status_code, 200)
        fetch.assert_not_called()

    @patch('core.articles.overpass.fetch_relation_geometry')
    def test_overpass_outage_does_not_break_save(self, fetch):
        fetch.side_effect = OverpassError('boom')
        self.assertEqual(self._put().status_code, 200)
        self.place.refresh_from_db()
        self.assertIsNone(self.place.geometry)
        self.assertEqual(Revision.objects.count(), 1)


class AuthApiTests(TestCase):
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

    def test_headless_signup_and_session(self):
        response = self.client.post(
            '/_allauth/browser/v1/auth/signup',
            {'username': 'newuser', 'password': 'sturdy-passphrase-9'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(reverse('core:me')).json()['user']['username'],
            'newuser',
        )

    def test_headless_login_logout(self):
        User.objects.create_user('drew', password='sturdy-passphrase-9')
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
