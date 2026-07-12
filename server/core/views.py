from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import resolve as resolution
from .overpass import OverpassError


@api_view(['GET'])
def health(request):
    return Response({'status': 'ok'})


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
