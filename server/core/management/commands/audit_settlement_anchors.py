"""Find settlement Places anchored to an administrative area instead.

A settlement and the administrative area named after it are two entities
that share a name and often a footprint, and until the entity rung was
added to `choose_element` the name+radius query could not tell them
apart: the relation outranked the settlement's own node, so a click on
the city's label wrote the province's article. Havana resolved to Q12588
(Havana Province) rather than Q1563, Panama City to Q804 (the country).

This re-runs the *current* selection rule against live Overpass for every
settlement-class Place and reports the ones whose anchor would now come
out differently. Read-only: it names rows to delete and re-resolve, and
never writes.

The proximity cache in `resolve()` matches on display name, class and
distance, so a wrong row keeps being returned for the same click even
after the rule is fixed. Deleting it is what lets the click re-resolve.
"""

import time

from django.core.management.base import BaseCommand

from core import overpass
from core.models import Place

# Overpass etiquette: one name query per suspect row, spaced out. The
# public instance hands out slots rather than a rate, so a long sweep that
# starts collecting 429s wants a bigger gap (--interval), not a retry.
QUERY_INTERVAL_S = 2
# The widest a click can be: a low-zoom click is what swept the
# administrative area's boundary ways in, so audit at that same reach.
AUDIT_RADIUS_M = overpass.MAX_RADIUS_M
# A common name over that radius is a far heavier query than a click, and
# the client's 10 s budget 504s on it. Nobody is waiting on this one.
QUERY_TIMEOUT_S = 90
# `id(...)` filters per type; Overpass caps a query's length long before
# it caps this, but chunking keeps the tag lookup to a handful of calls.
ID_CHUNK = 200


class Command(BaseCommand):
    help = (
        'Report settlement Places anchored to an administrative area of a '
        'different entity (read-only).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--slug', action='append', dest='slugs',
            help='Limit to these slugs (repeatable).',
        )
        parser.add_argument(
            '--interval', type=float, default=QUERY_INTERVAL_S,
            help=(
                f'Seconds between Overpass calls '
                f'(default {QUERY_INTERVAL_S}; raise it if you collect 429s).'
            ),
        )
        parser.add_argument(
            '--verbose-skips', action='store_true',
            help='Also list rows that were skipped and why.',
        )

    def handle(self, *args, **options):
        places = (
            Place.objects
            .filter(feature_class__in=overpass.SETTLEMENT_PLACES)
            .exclude(osm_type='')
            .exclude(osm_type=None)
            .order_by('slug')
        )
        if options['slugs']:
            places = places.filter(slug__in=options['slugs'])
        places = list(places)
        self.interval = options['interval']

        self.stdout.write(f'{len(places)} settlement-class place(s) anchored '
                          f'to an OSM element.')
        tags_by_key = self._fetch_tags(places)

        affected = []
        unchecked = []
        checked = skipped = 0
        for place in places:
            tags = tags_by_key.get((place.osm_type, place.osm_id))
            if tags is None:
                skipped += 1
                if options['verbose_skips']:
                    self.stdout.write(
                        f'  skip {place.slug}: {place.osm_type}/'
                        f'{place.osm_id} not found in OSM'
                    )
                continue
            # Only an administrative anchor can be the wrong half of this
            # pair; a settlement anchored to its own node or place relation
            # was never at risk.
            if not overpass._is_admin_area({'tags': tags}):
                skipped += 1
                if options['verbose_skips']:
                    self.stdout.write(
                        f'  skip {place.slug}: anchor is not an '
                        f'administrative area'
                    )
                continue

            name = tags.get('name')
            point = place.label_point or place.centroid
            if not name or point is None:
                skipped += 1
                continue

            checked += 1
            try:
                elements = self._candidates(name, point)
            except overpass.OverpassError as exc:
                # One busy instance must not cost the whole sweep; the row
                # is reported as unchecked and picked up on a re-run.
                checked -= 1
                unchecked.append(place.slug)
                self.stdout.write(self.style.ERROR(
                    f'  ? {place.slug}: Overpass failed ({exc})'
                ))
                time.sleep(self.interval)
                continue
            chosen = overpass.choose_element(elements, place.feature_class)
            time.sleep(self.interval)
            if chosen is None:
                continue
            chosen_qid = overpass.qid_of(chosen)
            if chosen_qid == place.wikidata_qid:
                continue

            affected.append((place, tags, chosen, chosen_qid))
            self.stdout.write(self.style.WARNING(
                f'  {place.slug} ({place.display_name}, '
                f'{place.feature_class})'
            ))
            self.stdout.write(
                f'      now: {place.osm_type}/{place.osm_id} '
                f'{place.wikidata_qid} '
                f'(admin_level={tags.get("admin_level")})'
            )
            self.stdout.write(
                f'      would resolve to: {chosen["type"]}/{chosen["id"]} '
                f'{chosen_qid} '
                f'(place={chosen.get("tags", {}).get("place")})'
            )

        self.stdout.write('')
        self.stdout.write(
            f'checked {checked}, skipped {skipped}, '
            f'{len(affected)} would change.'
        )
        if unchecked:
            self.stdout.write(self.style.ERROR(
                f'{len(unchecked)} unchecked (Overpass failed): '
                f'{" ".join(unchecked)} — re-run with --slug for these.'
            ))
        if affected:
            slugs = ' '.join(place.slug for place, _, _, _ in affected)
            self.stdout.write('')
            self.stdout.write('Slugs to delete and re-resolve:')
            self.stdout.write(f'  {slugs}')

    def _candidates(self, name, point):
        """`fetch_elements`, narrowed to entities OSM can name.

        The audit asks one question — does the anchor's QID change — and
        `["wikidata"]` is enough to answer it. Where a settlement seed
        exists it carries a QID and outranks every untagged element
        anyway; where none does, the QID rung decides and untagged
        elements never surface. What the filter buys is weight: over the
        full 10 km click radius, "Paris" is 35 elements in 8.5 s
        unfiltered against 4 in 3.2 s with it, and the unfiltered sweep
        was losing rows to 504s.
        """
        query = (
            f'[out:json][timeout:{QUERY_TIMEOUT_S}];'
            f'nwr["name"="{overpass._escape(name)}"]["wikidata"]'
            f'(around:{AUDIT_RADIUS_M},{point.y},{point.x});'
            'out tags bb;'
        )
        return overpass._call(query, timeout_s=QUERY_TIMEOUT_S + 5)

    def _fetch_tags(self, places):
        """{(osm_type, osm_id): tags} for every anchor, in a few calls."""
        wanted = {}
        for place in places:
            wanted.setdefault(place.osm_type, set()).add(place.osm_id)

        tags_by_key = {}
        for osm_type, ids in wanted.items():
            ids = sorted(ids)
            for start in range(0, len(ids), ID_CHUNK):
                chunk = ids[start:start + ID_CHUNK]
                query = (
                    '[out:json][timeout:60];'
                    f'{osm_type}(id:{",".join(str(i) for i in chunk)});'
                    'out tags;'
                )
                # Private on purpose: this is an audit tool reaching past
                # the client's own query vocabulary, not a new capability
                # the app needs.
                for element in overpass._call(query, timeout_s=65):
                    tags_by_key[(element['type'], element['id'])] = (
                        element.get('tags', {})
                    )
                time.sleep(self.interval)
        return tags_by_key
