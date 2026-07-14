import json

from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.geos import Polygon
from django.db.models import Q
from django.db.models.functions import Cast
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import resolve as resolution
from .articles import save_edit
from .models import Place
from .overpass import OverpassError
from .serializers import ArticleEditSerializer

MAX_HIGHLIGHTS = 500


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
                'geometry': json.loads(place.centroid.geojson),
                'properties': {
                    'slug': place.slug,
                    'display_name': place.display_name,
                    'feature_class': place.feature_class,
                    'names': sorted(names),
                },
            }
        )
    return Response({'type': 'FeatureCollection', 'features': features})


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
