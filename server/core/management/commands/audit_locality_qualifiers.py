"""Find Places whose slug was qualified by a coarser name than the rung
would give today.

Until 2026-08-24 a failed Overpass call in the locality rung was swallowed:
`_locality_for` returned whatever containment had (a district), or nothing,
and the mint carried on. The result was a permanent slug naming somewhere
the place is not. `high-street-doncaster` was minted for a street in
Mexborough that way — the settlement-node rung was unreachable, and the slug
it should have had went unused while Doncaster's own High Street fell to
`high-street-doncaster-2`.

The rung now raises instead (see `resolve._locality_for`), so no new row can
be minted this way. This finds the ones already in the database.

**Read-only.** It names rows to delete and re-resolve; `DATABASE.md` §9 is
the delete, and it only frees a slug while prelaunch is open.

## What counts as degraded

`unique_slug` tries `base`, then `base-locality`, then
`base-locality-qualifier`, then a numeric tail. A slug of the form
`base-qualifier` is therefore only reachable when the locality was **None**
at mint time. So: recompute the locality now, and if the stored slug is not
on the ladder that locality produces, the rung has an answer it did not have
then.

Reported separately from that: rows where the slug it *should* hold is
already taken by another place. Those are ordinary collisions, not damage,
and deleting them frees nothing.
"""

import time

from django.core.management.base import BaseCommand

from core import overpass
from core import resolve as resolution
from core.admin_areas import (
    admin_qualifier,
    locality_qualifier,
    nearest_admin_area,
)
from core.models import Place, PlaceSlug
from core.slugs import slugify, transliterate

# Overpass etiquette, as `audit_settlement_anchors`: one row's worth of
# calls, then a pause. A row costs up to three calls (containing areas, the
# probe intersection for anything long, the settlement nodes), so a sweep of
# the whole corpus is a few hundred requests against one IP — pace it.
QUERY_INTERVAL_S = 2


class Command(BaseCommand):
    help = (
        'Report Places whose slug qualifier is coarser than the locality '
        'rung would give today (read-only).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--slug', action='append', dest='slugs',
            help='Limit to these slugs (repeatable).',
        )
        parser.add_argument(
            '--interval', type=float, default=QUERY_INTERVAL_S,
            help=(
                f'Seconds between rows '
                f'(default {QUERY_INTERVAL_S}; raise it if you collect 429s).'
            ),
        )
        parser.add_argument(
            '--verbose-skips', action='store_true',
            help='Also list rows that were skipped and why.',
        )

    def handle(self, *args, **options):
        places = Place.objects.order_by('slug')
        if options['slugs']:
            places = places.filter(slug__in=options['slugs'])
        places = list(places)
        interval = options['interval']
        verbose = options['verbose_skips']

        self.stdout.write(f'{len(places)} place(s) to check.')

        degraded = []
        blocked = []
        unchecked = []
        checked = skipped = 0

        for place in places:
            point = place.label_point or place.centroid
            if point is None:
                skipped += 1
                if verbose:
                    self.stdout.write(f'  skip {place.slug}: no point')
                continue

            base = slugify(transliterate(place.display_name))[:100] or 'place'
            if place.slug == base:
                # The incumbent keeps the bare slug by design, whatever the
                # rung would say now: nothing was qualified, so nothing was
                # degraded.
                skipped += 1
                if verbose:
                    self.stdout.write(f'  skip {place.slug}: bare slug')
                continue

            try:
                locality_raw = self._locality_now(place, point)
            except overpass.OverpassError as exc:
                unchecked.append(place.slug)
                self.stdout.write(self.style.ERROR(
                    f'  ? {place.slug}: Overpass failed ({exc})'
                ))
                time.sleep(interval)
                continue
            time.sleep(interval)

            checked += 1
            area = nearest_admin_area(point)
            locality = locality_qualifier(
                locality_raw, place.display_name, area
            )
            if locality is None:
                # No locality now either: the qualifier it has is the one
                # the ladder still produces.
                continue

            qualifier = admin_qualifier(
                area, place.display_name, place.wikidata_qid,
                place.feature_class,
            )
            ladder = [base, f'{base}-{locality}']
            if qualifier:
                ladder.append(f'{ladder[-1]}-{qualifier}')
            if place.slug in ladder:
                continue

            wanted = f'{base}-{locality}'
            holder = (
                PlaceSlug.objects
                .filter(slug=wanted)
                .exclude(place_id=place.id)
                .first()
            )
            if holder is not None:
                blocked.append((place, wanted, holder))
                continue

            degraded.append((place, wanted))
            self.stdout.write(self.style.WARNING(
                f'  {place.slug} ({place.display_name}, '
                f'{place.feature_class})'
            ))
            self.stdout.write(f'      would now be: {wanted}')

        self.stdout.write('')
        self.stdout.write(
            f'checked {checked}, skipped {skipped}, '
            f'{len(degraded)} degraded, {len(blocked)} blocked.'
        )
        if blocked:
            self.stdout.write('')
            self.stdout.write(
                'Blocked — the better slug is already another place\'s, so '
                'these are ordinary collisions rather than damage:'
            )
            for place, wanted, holder in blocked:
                self.stdout.write(
                    f'  {place.slug} -> {wanted} (held by {holder.place.slug})'
                )
        if unchecked:
            self.stdout.write(self.style.ERROR(
                f'{len(unchecked)} unchecked (Overpass failed): '
                f'{" ".join(unchecked)} — re-run with --slug for these.'
            ))
        if degraded:
            slugs = ' '.join(place.slug for place, _ in degraded)
            self.stdout.write('')
            self.stdout.write('Slugs to delete and re-resolve:')
            self.stdout.write(f'  {slugs}')

    def _locality_now(self, place, point):
        """Re-run the locality rung against live Overpass.

        Private helpers on purpose, as in `audit_settlement_anchors`: this
        replays the mint's own reasoning rather than adding a second
        implementation of it that could drift from the real one.

        The click is gone, so the label point stands in for it — it is the
        point the mint qualified from anyway (`SLUGS.md` §2b). The
        containing areas are fetched only when the feature is too short to
        probe; a longer one has `_areas_containing_all_of` fetch its own
        intersection, and asking here as well would buy a wasted call.
        """
        if resolution._probe_set(place.geometry) is None:
            click_areas = overpass.fetch_common_areas([point])
        else:
            click_areas = []
        return resolution._locality_for(
            place.geometry, point, point, click_areas
        )
