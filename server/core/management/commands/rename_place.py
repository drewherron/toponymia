"""Rename a place's canonical slug, leaving the old one as a 301 alias.

The admin surface for the one thing the app has never been able to do: change a
slug without breaking every
link that points at the old one. The old slug is kept as an alias and 301s to the
new canonical, so sitemap entries, shared permalinks, and in-article cross-links
all keep resolving.

v1 scope: the target must be *free*. Reclaiming a slug held by another place (the
prominence question — taking `ojai` from a restaurant for the city) is out of
scope; it repoints live URLs and wants a human judgement, so it stays a shell
operation. Renaming to a slug this same place already holds as an alias is
allowed and simply promotes that alias to canonical.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.http import Http404
from django.utils.text import slugify

from core.models import PlaceSlug
from core.slugs import place_by_slug


class Command(BaseCommand):
    help = "Change a place's canonical slug, keeping the old one as a 301 alias."

    def add_arguments(self, parser):
        parser.add_argument(
            'old', help='A current slug of the place (canonical or alias).'
        )
        parser.add_argument('new', help='The new canonical slug.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing.',
        )

    def handle(self, *args, **options):
        old = options['old']
        new = options['new']
        dry_run = options['dry_run']

        # Identify the place from any slug it answers to.
        try:
            place = place_by_slug(old)
        except Http404:
            raise CommandError(
                f"No place answers to the slug '{old}'."
            ) from None

        if slugify(new) != new or not new:
            raise CommandError(
                f"'{new}' is not a valid slug (expected '{slugify(new) or ''}')."
            )

        if new == place.slug:
            raise CommandError(
                f"'{new}' is already {place.display_name}'s canonical slug."
            )

        existing = PlaceSlug.objects.filter(slug=new).first()
        if existing is not None and existing.place_id != place.id:
            other = existing.place
            raise CommandError(
                f"'{new}' is held by {other.display_name} "
                f"(pk={other.id}, slug='{other.slug}'). Reclaiming a slug from "
                f"another place is not supported here — see docs/slug-renames.md."
            )

        promoting = existing is not None  # an alias of THIS place → promote
        action = (
            f"promote alias '{new}' to canonical (was '{place.slug}')"
            if promoting
            else f"rename '{place.slug}' -> '{new}' (old kept as 301 alias)"
        )
        self.stdout.write(f'{place.display_name} (pk={place.id}): {action}')

        if dry_run:
            self.stdout.write(self.style.WARNING('dry run — nothing written.'))
            return

        with transaction.atomic():
            # Demote whatever is canonical now; it stays as an alias so its URL
            # keeps 301ing to the new canonical.
            PlaceSlug.objects.filter(
                place=place, is_canonical=True
            ).update(is_canonical=False)
            if promoting:
                existing.is_canonical = True
                existing.save(update_fields=['is_canonical'])
            else:
                PlaceSlug.objects.create(
                    place=place, slug=new, is_canonical=True
                )
            place.slug = new
            place.save(update_fields=['slug'])

        self.stdout.write(self.style.SUCCESS(f'Done. Canonical is now {new}.'))
