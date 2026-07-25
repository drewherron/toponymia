"""Re-snap label points onto cached geometry (DESIGN.md §3.1).

Existing rows took their label point from the creating click, which for a
long feature sits wherever the first resolver happened to touch it — a
river seeded from Wikidata got its *source* (the Mississippi's dot sat in
northern Minnesota). This recomputes it as the midpoint of the cached
course.

Line-like relations resolved before that change cached no geometry at
all; --fetch re-fetches it from Overpass for those. Without the flag the
command is offline and touches only rows that already have geometry.
"""

import time

from django.core.management.base import BaseCommand

from core import resolve
from core.models import Place

# Overpass etiquette: these are big `out geom` calls, so space them out.
FETCH_INTERVAL_S = 3


class Command(BaseCommand):
    help = 'Recompute Place.label_point as the midpoint of cached geometry.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--fetch', action='store_true',
            help='Also fetch geometry for line-like relations missing it.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would move without writing.',
        )
        parser.add_argument(
            '--slug', action='append', dest='slugs',
            help='Limit to these slugs (repeatable).',
        )

    def handle(self, *args, **options):
        places = Place.objects.all().order_by('slug')
        if options['slugs']:
            places = places.filter(slug__in=options['slugs'])

        moved = skipped = fetched = 0
        for place in places:
            if place.osm_type == 'node':
                continue

            if (
                place.geometry is None
                and options['fetch']
                and place.osm_type == 'relation'
            ):
                geometry = resolve._relation_geometry(place.osm_id)
                if geometry is not None:
                    place.geometry = geometry
                    fetched += 1
                time.sleep(FETCH_INTERVAL_S)

            if place.geometry is None:
                skipped += 1
                continue

            point = resolve.representative_point(place.geometry)
            if point is None:
                skipped += 1
                continue

            before = place.label_point
            distance_km = (
                point.distance(before) * 111 if before else None
            )
            self.stdout.write(
                f'{place.slug}: {_fmt(before)} -> {_fmt(point)}'
                + (f'  ({distance_km:.0f} km)' if distance_km else '')
            )
            if not options['dry_run']:
                place.label_point = point
                place.save(update_fields=['geometry', 'label_point'])
            moved += 1

        self.stdout.write(self.style.SUCCESS(
            f'{moved} re-snapped, {fetched} geometries fetched, '
            f'{skipped} skipped (no geometry)'
            + (' [dry run]' if options['dry_run'] else '')
        ))


def _fmt(point):
    return f'({point.x:.4f}, {point.y:.4f})' if point else 'none'
