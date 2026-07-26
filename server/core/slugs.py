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


def unique_slug(display_name):
    """A slug used by no Place yet — canonical or alias. Counts up on
    collision (`ojai`, `ojai-2`, `ojai-3`), skipping slugs already parked as
    aliases so a fresh place never lands on one that redirects elsewhere."""
    base = slugify(display_name)[:100] or 'place'
    slug = base
    n = 2
    while PlaceSlug.objects.filter(slug=slug).exists():
        slug = f'{base}-{n}'
        n += 1
    return slug
