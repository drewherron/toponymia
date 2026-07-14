"""Article write path: one transaction creates the Revision, moves the
current pointer, and rematerializes PlaceName rows (DESIGN.md §4 — one
write path, no sync ambiguity)."""

from django.contrib.gis.geos import LineString, MultiLineString
from django.db import transaction

from . import overpass
from .models import Article, PlaceName, Revision

# ~20 m at the equator: enough to shrink a river relation's member ways
# to a paintable size without visible drift at highlight zooms.
SIMPLIFY_TOLERANCE_DEG = 0.0002


def save_edit(place, author, content, comment):
    """Apply a validated content snapshot as a new revision. Returns it."""
    with transaction.atomic():
        article, _ = Article.objects.get_or_create(place=place)
        revision = Revision.objects.create(
            article=article, author=author, comment=comment, content=content
        )
        article.current_revision = revision
        article.save(update_fields=['current_revision'])
        _materialize_names(place, content.get('names', []))
    return revision


def ensure_geometry(place):
    """Backfill paintable geometry for a relation-anchored place.

    Resolve only caches centroid+bbox for relations (member geometry can
    be huge), so the first article save fetches and simplifies it for the
    highlight overlay. Best-effort: on Overpass failure the place keeps a
    null geometry and highlights fall back to the centroid.
    """
    if place.geometry is not None or place.osm_type != 'relation':
        return
    try:
        parts = overpass.fetch_relation_geometry(place.osm_id)
    except overpass.OverpassError:
        return
    if not parts:
        return
    lines = MultiLineString(
        [LineString(part) for part in parts], srid=4326
    )
    simplified = lines.simplify(SIMPLIFY_TOLERANCE_DEG)
    if simplified.empty:
        simplified = lines
    simplified.srid = 4326
    place.geometry = simplified
    place.save(update_fields=['geometry'])


def _materialize_names(place, names):
    PlaceName.objects.filter(place=place).delete()
    seen = set()
    rows = []
    for entry in names:
        key = (entry['name'], entry.get('language', ''))
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            PlaceName(
                place=place,
                name=entry['name'],
                language=entry.get('language', ''),
                is_endonym=entry.get('is_endonym', False),
                from_languages=entry.get('from_languages', []),
            )
        )
    PlaceName.objects.bulk_create(rows)
