from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import LineString, Point, Polygon
from django.test import TestCase
from django.urls import reverse

from .models import Article, Place, PlaceName, Report, Revision, TalkPost
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
        # dots hang at the click, the only point known to be ON the river
        self.assertEqual(
            (db_place.label_point.x, db_place.label_point.y), (-91.0, 32.0)
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


class RevisionApiTests(TestCase):
    def setUp(self):
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


class TalkApiTests(TestCase):
    def setUp(self):
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


class SearchApiTests(TestCase):
    def setUp(self):
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


class RandomApiTests(TestCase):
    def setUp(self):
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


class ModerationApiTests(TestCase):
    def setUp(self):
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

    def test_report_unknown_target_404s(self):
        self.client.force_login(self.other)
        self.assertEqual(
            self._report('talk_post', 99999).status_code, 404
        )

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
