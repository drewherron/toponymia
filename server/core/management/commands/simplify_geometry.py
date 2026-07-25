"""Thin stored Place.geometry to the on-write tolerance (DESIGN.md §3.2).

Rows resolved before geometry was simplified carry every vertex Overpass
returned — the Mississippi's course is 18,193 of them, 292 kB. Nothing
draws or measures at that resolution: the geometry is only ever used as a
spatial filter, and the highlight overlay emits label_point.

Offline and idempotent — re-running finds nothing left to thin.
"""

from django.core.management.base import BaseCommand

from core.models import Place
from core.resolve import simplified


class Command(BaseCommand):
    help = 'Simplify stored Place.geometry to the standard tolerance.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report the savings without writing.',
        )

    def handle(self, *args, **options):
        before_total = after_total = 0
        changed = 0

        for place in Place.objects.exclude(geometry=None).order_by('slug'):
            before = len(place.geometry.wkb)
            thinned = simplified(place.geometry)
            after = len(thinned.wkb)
            before_total += before
            after_total += after
            if after >= before:
                continue

            # label_point is deliberately left alone: it was snapped to the
            # full course, which is what the basemap draws. Re-snapping it
            # onto this thinned copy would move it 63 km on the Mississippi
            # (thinning eats meander length unevenly) to buy nothing. This
            # gap is the expected residue, not a defect.
            gap_m = thinned.distance(place.label_point) * 111_000
            self.stdout.write(
                f'{place.slug}: {before / 1024:.0f} kB -> {after / 1024:.0f} kB'
                f'  ({after / before * 100:.0f}%, label_point {gap_m:.0f} m '
                'from thinned line)'
            )
            if not options['dry_run']:
                place.geometry = thinned
                place.save(update_fields=['geometry'])
            changed += 1

        saved = before_total - after_total
        self.stdout.write(self.style.SUCCESS(
            f'{changed} simplified, {before_total / 1024:.0f} kB -> '
            f'{after_total / 1024:.0f} kB (saved {saved / 1024:.0f} kB)'
            + (' [dry run]' if options['dry_run'] else '')
        ))
