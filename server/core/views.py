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
