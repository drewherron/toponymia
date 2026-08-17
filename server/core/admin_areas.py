"""What contains a point — the source of slug qualifiers.

`portland-oregon` instead of `portland-2`: when a name collides, the slug is
disambiguated by the administrative area the place sits in, looked up locally
against the Natural Earth table (`AdminArea`, loaded by
`load_admin_boundaries`). No network, no rate limit, ~1.5 ms indexed.

Two rungs, in order: the containing **subdivision**, then its **country**,
then nothing — and `unique_slug` falls back to a numeric suffix when this
returns None. Minting must never block or fail on a qualifier, so every path
here returns None rather than raising.

The subdivision tier is deliberately *not* consistent worldwide: NE's
admin-1 layer is US states, Jamaican parishes and English counties alike, and
we take whatever it gives. `portland-oregon`, `portland-dorset` and
`portland-jamaica` are all good slugs; a uniform tier would buy nothing a
reader can see.
"""

import logging

from django.contrib.gis.db.models.functions import Distance, GeometryDistance
from django.utils.text import slugify

from .models import AdminArea
from .slugs import transliterate

logger = logging.getLogger(__name__)

# How far off a boundary a point may sit and still be qualified by it.
#
# This is not slack for sloppy clicks — it is for NE's own coastlines, which
# are generalised inland far enough that harbour towns and islands fall
# outside their own subdivision: Tromsø by 1.6 km, Nuuk by 0.6 km, Venice
# and the Isle of Portland by ~0.1 km. Without a tolerance every one of them
# silently takes the numeric suffix, which is precisely the outcome this
# module exists to avoid.
#
# 50 km is far above the worst gap measured and far below anything that
# could reach across open water: a mid-Atlantic click is 912 km from the
# Azores and a mid-Pacific one 709 km from Kiribati, so both still decline.
MAX_DISTANCE_M = 50_000

# Belt for the slug length budget: 100-char base + '-' + 45 = 146, inside
# the 150-char columns. The longest real NE name_en is 47 characters
# ('Autonomous Region in Muslim Mindanao'-class), so this almost never bites.
QUALIFIER_MAX_CHARS = 45

# NE stores formal long-form country names. Where a genuinely common short
# form exists, use it — the qualifier is a distinguisher, not an address, and
# `georgia-usa` reads better than `georgia-united-states-of-america`.
#
# Only forms people actually use. Invented abbreviations are worse than the
# long name: 'CAR' for Central African Republic reads as a common noun,
# 'PNG' as an image format, 'SA' is ambiguous between South Africa and Saudi
# Arabia — so those countries keep their full names.
COUNTRY_ALIASES = {
    'United States of America': 'USA',
    'United Kingdom': 'UK',
    'United Arab Emirates': 'UAE',
    'Democratic Republic of the Congo': 'DRC',
    'Republic of the Congo': 'Congo',
    'United Republic of Tanzania': 'Tanzania',
    'Republic of Serbia': 'Serbia',
    'Czech Republic': 'Czechia',
    'Federated States of Micronesia': 'Micronesia',
    'The Bahamas': 'Bahamas',
}


def _fragment(name):
    """Slug-safe form of an area name, or None if nothing survives."""
    return slugify(transliterate(name or ''))[:QUALIFIER_MAX_CHARS] or None


def nearest_admin_area(point):
    """The AdminArea whose boundary is closest to `point`, or None.

    Nearest-within-tolerance rather than point-in-polygon — see
    MAX_DISTANCE_M. Contained points are simply the case where the distance
    is zero, so this subsumes containment and sidesteps the question of
    whether a point exactly on a border is 'inside' either neighbour.

    Returns None on any failure, including an unloaded table: a box where
    `load_admin_boundaries` has not run mints unqualified slugs rather than
    refusing to mint.
    """
    if point is None:
        return None
    try:
        area = (
            AdminArea.objects
            # Distance() gives metres for the tolerance test; the ORDER BY
            # is GeometryDistance because only `<->` uses the GiST index.
            .annotate(distance=Distance('geometry', point))
            .order_by(GeometryDistance('geometry', point))
            .first()
        )
    except Exception:
        logger.exception('Admin area lookup failed; minting unqualified')
        return None
    if area is None or area.distance.m > MAX_DISTANCE_M:
        return None
    return area


def admin_qualifier(area, name, qid=None):
    """A slug fragment naming what contains a place, or None.

    Subdivision, else country, else None. `name` and `qid` are the place
    being minted, needed for the self-reference guard: a place that *is* its
    own subdivision must not mint `oregon-oregon`, so it falls through to the
    country instead — which is exactly what turns Portland Parish into
    `portland-jamaica`. Falling through, not skipping to the numeric floor,
    matters because same-name-as-subdivision is common rather than exotic.
    """
    if area is None:
        return None
    fragment = _fragment(name)
    aliased, raw = _country_fragments(area)

    # The place IS the country. Nothing here can distinguish it: the country
    # rung would mint `france-france`, and the subdivision rung is worse
    # still, stamping a whole country with one of its own departments
    # (`france-nord`). Take the numeric floor instead.
    if fragment is not None and fragment in {aliased, raw}:
        return None

    subdivision = _fragment(area.subdivision)
    if subdivision and not _is_same_place(area, name, qid):
        return subdivision

    return aliased


def _country_fragments(area):
    """The country's slug fragment as used, and as NE spells it.

    Both, because the self-reference check has to catch a place named
    'United States of America' as readily as one named 'USA' — comparing
    only the aliased form would let the first through to `usa`.
    """
    raw = _fragment(area.country)
    aliased = _fragment(COUNTRY_ALIASES.get(area.country, area.country))
    return aliased, raw


def _is_same_place(area, name, qid):
    """Is the place being minted the subdivision itself?

    QID first — identity beats spelling, and NE carries `wikidataid` for most
    rows. Name comparison is the fallback, in slug space so that Reykjavík
    and Reykjavik are recognised as the same claim.
    """
    if qid and area.wikidata_qid and qid == area.wikidata_qid:
        return True
    fragment = _fragment(name)
    return fragment is not None and fragment in {
        _fragment(area.subdivision), _fragment(area.subdivision_local)
    }


def qualifier_for(point, name, qid=None):
    """Convenience for the mint paths: look up and qualify in one call.

    Prefer the two-step form where the caller also wants the AdminArea
    itself (to store admin context on the Place) — this exists so a caller
    that only needs the slug fragment doesn't have to know about areas.
    """
    return admin_qualifier(nearest_admin_area(point), name, qid)
