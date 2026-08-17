"""Load the Natural Earth admin-1 layer into the AdminArea table.

The table only exists to qualify slugs (`portland-oregon`, not `portland-2`),
so this is a wholesale replace: truncate, reload, done. Nothing in the app
writes AdminArea, so there is nothing to merge and no history to keep.

Run it once after deploy, and again whenever the data file is refreshed.

DATA FILE — `core/data/ne_10m_admin_1.geojson.gz`, **Natural Earth 5.1.1**,
committed to the repo. It is public domain, so there is no attribution or
provenance burden, and committing it keeps deploys deterministic instead of
depending on naturalearthdata.com being up on the day the box is rebuilt.

To refresh it, take `ne_10m_admin_1_states_provinces` from
naturalearthdata.com and re-run exactly:

    ogr2ogr -f GeoJSON admin1.geojson ne_10m_admin_1_states_provinces.shp \
      -select name,name_en,admin,adm0_a3,iso_a2,iso_3166_2,wikidataid,geonunit,type_en \
      -nlt MULTIPOLYGON -simplify 0.001 -lco COORDINATE_PRECISION=5
    gzip -9 admin1.geojson

That drops 112 unused columns and thins the geometry to ~110 m, comfortably
inside NE 1:10m's own precision — spot-checked point-for-point against the
raw shapefile with identical results — and lands at 7.3 MB, half the size of
the upstream zip. GDAL reads it in place through `/vsigzip/`, so there is no
unpack step and the committed file is the file that loads.
"""

import gc

from django.contrib.gis.gdal import DataSource
from django.contrib.gis.geos import MultiPolygon
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import AdminArea

DATA_PATH = 'core/data/ne_10m_admin_1.geojson.gz'

# NE writes '-1' where it has no ISO code (the Sovereign Base Areas,
# Baykonur, Guantanamo). Store blank rather than propagating a sentinel.
NO_VALUE = {'-1', '-99', ''}

# Rows arrive one at a time from GDAL but insert far better in batches;
# 500 keeps peak memory modest on a small box while cutting the round
# trips by three orders of magnitude.
BATCH_SIZE = 500


def _clean(value):
    value = (value or '').strip()
    return '' if value in NO_VALUE else value


def _subdivision_type(feature):
    """NE's word for this tier: 'Parish', 'Governorate', 'State'.

    Some rows offer alternatives pipe-joined ('Commune|Municipality');
    take the first, which is the one NE lists as primary.
    """
    return _clean(feature.get('type_en')).split('|')[0].strip()


class Command(BaseCommand):
    help = 'Load Natural Earth admin-1 boundaries into AdminArea.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--path', default=DATA_PATH,
            help=f'Override the data file (default: {DATA_PATH}).',
        )

    def handle(self, *args, **options):
        path = options['path']
        source = path if path.startswith('/vsi') else f'/vsigzip/{path}'
        try:
            layer = DataSource(source)[0]
        except Exception as exc:
            raise CommandError(f'Could not open {source}: {exc}') from exc

        total = len(layer)
        self.stdout.write(f'Loading {total} features from {path}…')

        # One transaction: a half-loaded boundary table would silently
        # qualify some mints and not others, which is worse than no table.
        with transaction.atomic():
            AdminArea.objects.all().delete()
            written = skipped = 0
            batch = []
            for feature in layer:
                row = self._to_row(feature)
                if row is None:
                    skipped += 1
                    continue
                batch.append(row)
                if len(batch) >= BATCH_SIZE:
                    AdminArea.objects.bulk_create(batch)
                    written += len(batch)
                    batch = []
                    # GDAL feature geometry is not cheap; don't let the
                    # whole planet accumulate before the loop ends.
                    gc.collect()
            if batch:
                AdminArea.objects.bulk_create(batch)
                written += len(batch)

        note = f' ({skipped} skipped: no usable geometry)' if skipped else ''
        self.stdout.write(self.style.SUCCESS(
            f'Loaded {written} admin areas{note}.'
        ))

    def _to_row(self, feature):
        geometry = feature.geom.geos
        if geometry.geom_type == 'Polygon':
            geometry = MultiPolygon(geometry, srid=geometry.srid)
        if geometry.geom_type != 'MultiPolygon' or geometry.empty:
            return None
        geometry.srid = 4326

        # 7 NE rows carry neither name nor name_en. They still load — the
        # country is the useful half of such a row, and the qualifier
        # ladder falls through a blank subdivision to it.
        return AdminArea(
            subdivision=_clean(feature.get('name_en'))
            or _clean(feature.get('name')),
            subdivision_local=_clean(feature.get('name')),
            subdivision_type=_subdivision_type(feature),
            country=_clean(feature.get('admin')),
            country_a3=_clean(feature.get('adm0_a3')),
            country_iso=_clean(feature.get('iso_a2')),
            subdivision_iso=_clean(feature.get('iso_3166_2')),
            wikidata_qid=_clean(feature.get('wikidataid')),
            geometry=geometry,
        )
