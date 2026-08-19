"""Slug lookup and minting — the alias-table plumbing.

A Place answers to its canonical slug and to any aliases left behind by renames
(operator guide: docs/slug-renames.md); this module turns a
slug string (either kind) into a Place, and picks a free slug for a new one.
The canonical PlaceSlug row itself is created by a post_save signal on Place
(models.py), so every creation path — resolve, tests, the shell — is covered.
"""

from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.text import slugify

from .models import Place, PlaceSlug


def place_by_slug(slug, queryset=None):
    """Resolve a canonical *or* alias slug to its Place, 404 if unknown.

    Pass a shaped `queryset` (e.g. with select_related/only) to keep each call
    site's query exactly as it was — the alias lookup only supplies the pk.
    """
    qs = Place.objects.all() if queryset is None else queryset
    match = PlaceSlug.objects.filter(slug=slug).first()
    if match is None:
        raise Http404('No place with that slug')
    return get_object_or_404(qs, pk=match.place_id)


# Latin letters that are letters in their own right, not accented bases.
# `slugify` runs NFKD then drops every non-ASCII byte: an accented letter
# decomposes to base + combining mark and survives (Kraków -> krakow), but
# these decompose to nothing and vanish silently — Tromsø minted `troms`,
# Ærø minted `r`. Nothing upstream saves us, because the names carrying
# them are already Latin script, so `name:en` (where it exists at all) is
# usually byte-identical to the native name.
#
# Mapped to what someone would have typed: Norwegian/Danish, Polish,
# Icelandic, Turkish dotless i, Maltese, Vietnamese/Croatian đ, German ß,
# and the Sámi letters we sit next to up in Tromsø. Values are strings,
# not characters — ß and þ each expand to two letters.
TRANSLITERATIONS = {
    'ø': 'o', 'Ø': 'O',
    'ł': 'l', 'Ł': 'L',
    'þ': 'th', 'Þ': 'Th',
    'ð': 'd', 'Ð': 'D',
    'æ': 'ae', 'Æ': 'Ae',
    'œ': 'oe', 'Œ': 'Oe',
    'ß': 'ss', 'ẞ': 'Ss',
    'ı': 'i',
    'ħ': 'h', 'Ħ': 'H',
    'đ': 'd', 'Đ': 'D',
    'ŧ': 't', 'Ŧ': 'T',
    'ŋ': 'n', 'Ŋ': 'N',
}

_TRANSLITERATION_TABLE = str.maketrans(TRANSLITERATIONS)


def transliterate(name):
    """Replace non-decomposable Latin letters with their ASCII equivalents.

    Apply before `slugify`, which would otherwise drop them. Names and
    qualifiers both go through it, so `portland-tromso` survives whole.
    """
    return name.translate(_TRANSLITERATION_TABLE)


def unique_slug(display_name, qualifier=None, locality=None):
    """A slug used by no Place yet — canonical or alias.

    The first place of a name keeps it bare (`portland`). A second one is
    disambiguated by `qualifier`, an already-slug-safe fragment naming what
    contains it (`portland-maine`; see core.admin_areas) — so nothing
    already published ever moves, at the price of the bare slug going to
    whoever was minted first. An operator settles that deliberately with
    `rename_place`, which leaves the bare slug behind as a 301.

    `locality` is the finer fragment tried *first* where one is known, which
    is what makes street names workable: `qualifier` alone can only reach the
    Natural Earth subdivision, and English counties hold many towns each, so
    every High Street in Lincolnshire collided into
    `high-street-lincolnshire-N`. With a locality the ladder reads
    `high-street` -> `high-street-lincoln` -> `high-street-lincoln-lincolnshire`.
    Note the escalation stacks the two rather than swapping one for the
    other: once the locality is in the slug, dropping back to the bare county
    would read as a *different, coarser* place rather than a more precise one.

    The caller decides how wide "locality" is by how much of the feature it
    asked about — see `resolve._areas_containing_all_of`. A street inside one
    village gets the village; a road through three gets the district.

    The numeric suffix stays as the unconditional floor, for no qualifier at
    all (`portland-2`) and for the most qualified form colliding too. It
    counts on that most qualified form, so two Portlands in Oregon give
    `portland-oregon-2` rather than `portland-3` — still ugly, but it tells
    the reader something.

    Alias slugs are skipped along with canonical ones, so a fresh place never
    lands on one that redirects elsewhere.
    """
    base = slugify(transliterate(display_name))[:100] or 'place'
    attempts = [base]
    if locality:
        attempts.append(f'{base}-{locality}')
    if qualifier:
        attempts.append(f'{attempts[-1]}-{qualifier}')

    for slug in attempts:
        if not PlaceSlug.objects.filter(slug=slug).exists():
            return slug

    stem = attempts[-1]
    n = 2
    while PlaceSlug.objects.filter(slug=f'{stem}-{n}').exists():
        n += 1
    return f'{stem}-{n}'
