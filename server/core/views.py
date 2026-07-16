import json

from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.geos import Polygon
from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.functions import Cast
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import resolve as resolution
from .articles import save_edit
from .models import Place, Revision, TalkPost, TalkThread
from .overpass import OverpassError
from .serializers import (
    ArticleEditSerializer,
    RevertSerializer,
    TalkPostSerializer,
    TalkThreadSerializer,
)

MAX_HIGHLIGHTS = 500
MAX_SEARCH_RESULTS = 8


@api_view(['GET'])
def health(request):
    return Response({'status': 'ok'})


@api_view(['GET'])
@ensure_csrf_cookie
def me(request):
    """Session probe for the SPA; also plants the CSRF cookie the client
    needs before it can POST to /api or /_allauth."""
    user = request.user
    if not user.is_authenticated:
        return Response({'user': None})
    return Response({'user': {'id': user.id, 'username': user.username}})


def _place_json(place):
    return {
        'id': place.id,
        'slug': place.slug,
        'display_name': place.display_name,
        'feature_class': place.feature_class,
        'anchor_level': place.anchor_level,
        'wikidata_qid': place.wikidata_qid,
        'osm_type': place.osm_type,
        'osm_id': place.osm_id,
        'centroid': [place.centroid.x, place.centroid.y],
        # A point guaranteed to lie on the feature — where search/deep
        # links should fly the map. bbox extent lets the client fit big
        # features (a river) instead of zooming to one point on them.
        'label_point': (
            [place.label_point.x, place.label_point.y]
            if place.label_point
            else None
        ),
        'bbox': list(place.bbox.extent) if place.bbox else None,
    }


@api_view(['POST'])
def resolve(request):
    data = request.data
    name = data.get('name')
    feature_class = data.get('class')
    lng_lat = data.get('lngLat')
    zoom = data.get('zoom')

    if (
        not isinstance(name, str) or not name.strip()
        or not isinstance(feature_class, str) or not feature_class
        or not isinstance(lng_lat, (list, tuple)) or len(lng_lat) != 2
        or not all(isinstance(c, (int, float)) for c in lng_lat)
        or not (isinstance(zoom, (int, float)) or zoom is None)
    ):
        return Response(
            {'error': 'expected {name, class, lngLat: [lng, lat], zoom?}'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    lng, lat = lng_lat
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        return Response(
            {'error': 'lngLat out of range'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        place, created = resolution.resolve(
            name.strip(), feature_class, lng, lat, zoom
        )
    except OverpassError:
        return Response(
            {'error': 'resolution service unavailable'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response({'place': _place_json(place), 'created': created})


@api_view(['GET'])
def highlights(request):
    """Places with articles in the viewport, as centroid GeoJSON.

    The client recolors basemap labels matching `names` (DESIGN.md §2.2)
    and paints dots at the centroids in all-articles mode. Inclusion is
    tested against cached geometry/bbox too, so a river whose centroid is
    far away still lights its labels inside the viewport.
    """
    raw = request.query_params.get('bbox', '')
    try:
        min_lng, min_lat, max_lng, max_lat = (
            float(value) for value in raw.split(',')
        )
    except ValueError:
        return Response(
            {'error': 'expected bbox=minLng,minLat,maxLng,maxLat'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    min_lat, max_lat = max(min_lat, -90.0), min(max_lat, 90.0)
    # A wrapped or antimeridian-crossing viewport degenerates to the full
    # longitude range: over-fetches a little, never misses a highlight.
    if not (-180 <= min_lng < max_lng <= 180):
        min_lng, max_lng = -180.0, 180.0
    if min_lat >= max_lat:
        return Response({'type': 'FeatureCollection', 'features': []})

    viewport = Polygon.from_bbox((min_lng, min_lat, max_lng, max_lat))
    viewport.srid = 4326
    # Cast the geography columns to planar geometry: a viewport is a
    # lon/lat rectangle, but geography edges are great-circle arcs, which
    # misbehave for boxes wider than 180 degrees.
    planar = GeometryField(srid=4326)
    places = Place.objects.filter(
        article__current_revision__isnull=False
    ).annotate(
        geometry_plane=Cast('geometry', planar),
        bbox_plane=Cast('bbox', planar),
        centroid_plane=Cast('centroid', planar),
    ).filter(
        Q(geometry_plane__intersects=viewport)
        | Q(bbox_plane__intersects=viewport)
        | Q(centroid_plane__intersects=viewport)
    ).prefetch_related('names')[:MAX_HIGHLIGHTS]

    features = []
    for place in places:
        names = {place.display_name}
        names.update(entry.name for entry in place.names.all())
        features.append(
            {
                'type': 'Feature',
                'geometry': json.loads(
                    (place.label_point or place.centroid).geojson
                ),
                'properties': {
                    'slug': place.slug,
                    'display_name': place.display_name,
                    'feature_class': place.feature_class,
                    'names': sorted(names),
                },
            }
        )
    return Response({'type': 'FeatureCollection', 'features': features})


@api_view(['GET'])
def search(request):
    """Find our own articles by any of their names (DESIGN.md §2.3).

    Only places with a published article are returned — the "everything
    else on Earth" half of the search box is the client-side geocoder.
    Matches the display name and the materialized PlaceNames, so the
    French exonym finds the place too; `matched_name` says which alias
    hit when the display name itself didn't.
    """
    query = request.query_params.get('q', '').strip()
    if len(query) < 2:
        return Response({'results': []})
    places = (
        Place.objects.filter(article__current_revision__isnull=False)
        .filter(
            Q(display_name__icontains=query)
            | Q(names__name__icontains=query)
        )
        .annotate(
            prefix_rank=Case(
                When(display_name__istartswith=query, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by('prefix_rank', 'display_name', 'id')
        .distinct()
        .prefetch_related('names')[:MAX_SEARCH_RESULTS]
    )
    lowered = query.lower()
    results = []
    for place in places:
        matched_name = None
        if lowered not in place.display_name.lower():
            matched_name = next(
                (
                    entry.name
                    for entry in place.names.all()
                    if lowered in entry.name.lower()
                ),
                None,
            )
        results.append({**_place_json(place), 'matched_name': matched_name})
    return Response({'results': results})


@api_view(['GET'])
def random_article(request):
    """A random place that has an article; null when the wiki is empty.
    order_by('?') is fine at wiki scale."""
    place = (
        Place.objects.filter(article__current_revision__isnull=False)
        .order_by('?')
        .first()
    )
    return Response({'place': _place_json(place) if place else None})


def _article_json(article):
    revision = article.current_revision
    if revision is None:
        return None
    return {
        'content': revision.content,
        'revision_id': revision.id,
        'author': revision.author.username,
        'created': revision.created.isoformat(),
        'comment': revision.comment,
        'protection_level': article.protection_level,
    }


@api_view(['GET'])
def place_detail(request, slug):
    place = get_object_or_404(
        Place.objects.select_related(
            'article__current_revision__author'
        ),
        slug=slug,
    )
    article = getattr(place, 'article', None)
    return Response(
        {
            'place': _place_json(place),
            'article': _article_json(article) if article else None,
        }
    )


def _revision_json(revision, current_id, with_content=False):
    data = {
        'id': revision.id,
        'author': revision.author.username,
        'created': revision.created.isoformat(),
        'comment': revision.comment,
        'is_current': revision.id == current_id,
    }
    if with_content:
        data['content'] = revision.content
    return data


@api_view(['GET'])
def revision_list(request, slug):
    """Edit history of a place's article, newest first. A place without
    an article has an empty history rather than a 404 — the History tab
    is shown for stubs too."""
    place = get_object_or_404(
        Place.objects.select_related('article'), slug=slug
    )
    article = getattr(place, 'article', None)
    if article is None:
        return Response({'revisions': []})
    revisions = article.revisions.select_related('author')
    return Response(
        {
            'revisions': [
                _revision_json(revision, article.current_revision_id)
                for revision in revisions
            ]
        }
    )


@api_view(['GET'])
def revision_detail(request, slug, revision_id):
    revision = get_object_or_404(
        Revision.objects.select_related('author', 'article'),
        id=revision_id,
        article__place__slug=slug,
    )
    return Response(
        {
            'revision': _revision_json(
                revision,
                revision.article.current_revision_id,
                with_content=True,
            )
        }
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def article_revert(request, slug):
    """Revert = a new revision copying an old snapshot (DESIGN.md §6)."""
    serializer = RevertSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    place = get_object_or_404(
        Place.objects.select_related('article'), slug=slug
    )
    article = getattr(place, 'article', None)
    old = get_object_or_404(
        Revision,
        id=serializer.validated_data['revision_id'],
        article__place__slug=slug,
    )
    if article.current_revision_id == old.id:
        return Response(
            {'error': 'already the current revision'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    comment = (
        serializer.validated_data['comment']
        or f'Reverted to revision {old.id}'
    )
    revision = save_edit(place, request.user, old.content, comment)
    return Response({'article': _article_json(revision.article)})


@api_view(['PUT'])
@permission_classes([IsAuthenticated])
def article_edit(request, slug):
    place = get_object_or_404(Place, slug=slug)
    serializer = ArticleEditSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    revision = save_edit(
        place,
        request.user,
        serializer.validated_data['content'],
        serializer.validated_data['comment'],
    )
    return Response({'article': _article_json(revision.article)})


def _post_json(post):
    return {
        'id': post.id,
        'author': post.author.username,
        'body_md': post.body_md,
        'created': post.created.isoformat(),
        'edited': post.edited.isoformat() if post.edited else None,
    }


def _thread_json(thread):
    return {
        'id': thread.id,
        'title': thread.title,
        'created': thread.created.isoformat(),
        'posts': [_post_json(post) for post in thread.posts.all()],
    }


@api_view(['GET', 'POST'])
def talk(request, slug):
    """Threaded discussion for a Place. GET is public; POST (new thread
    with its opening post) needs an account."""
    place = get_object_or_404(Place, slug=slug)
    if request.method == 'GET':
        threads = place.talk_threads.prefetch_related('posts__author')
        return Response(
            {'threads': [_thread_json(thread) for thread in threads]}
        )

    if not request.user.is_authenticated:
        return Response(status=status.HTTP_403_FORBIDDEN)
    serializer = TalkThreadSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    thread = TalkThread.objects.create(
        place=place, title=serializer.validated_data['title']
    )
    TalkPost.objects.create(
        thread=thread,
        author=request.user,
        body_md=serializer.validated_data['body_md'],
    )
    return Response(
        {'thread': _thread_json(thread)}, status=status.HTTP_201_CREATED
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def talk_reply(request, thread_id):
    thread = get_object_or_404(TalkThread, id=thread_id)
    serializer = TalkPostSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    post = TalkPost.objects.create(
        thread=thread,
        author=request.user,
        body_md=serializer.validated_data['body_md'],
    )
    return Response({'post': _post_json(post)}, status=status.HTTP_201_CREATED)


@api_view(['PUT'])
@permission_classes([IsAuthenticated])
def talk_post_edit(request, post_id):
    """Edit-own only (DESIGN.md §6); mods get more in M7."""
    post = get_object_or_404(TalkPost, id=post_id)
    if post.author_id != request.user.id:
        return Response(
            {'error': 'you can only edit your own posts'},
            status=status.HTTP_403_FORBIDDEN,
        )
    serializer = TalkPostSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    post.body_md = serializer.validated_data['body_md']
    post.edited = timezone.now()
    post.save(update_fields=['body_md', 'edited'])
    return Response({'post': _post_json(post)})
