"""Article write path: one transaction creates the Revision, moves the
current pointer, and rematerializes PlaceName rows (DESIGN.md §4 — one
write path, no sync ambiguity)."""

from django.db import transaction

from .models import Article, PlaceName, Revision


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
