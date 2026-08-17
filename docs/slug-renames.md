# Renaming a place's URL slug

Every place has a URL slug — `/place/ojai` — assigned once, automatically, when
the place is first resolved. It shows in the address bar, in the sitemap, in the
**Copy link** permalink, and in author-written cross-links (`[Ojai](/place/ojai)`).

When a name is already taken, the new place is disambiguated by the
administrative area it sits in — `portland-oregon`, `portland-maine` — or, for
an administrative area sharing its name with the place inside it, by its own
type (`havana-province`). A plain number, `ojai-2`, is the last resort when
neither applies.

Two things that still need a human, which is what this document is for:

- **The first place of a name keeps the bare slug**, whoever it happened to
  be. That is mint order, not editorial judgement, so if the wrong entity got
  there first — a restaurant called OJAI taking `ojai` before the town — the
  fix is to rename it and let the town have the name. The old slug stays
  behind as a redirect, so nothing breaks.
- **A typo or a later-corrected name** leaves the original spelling in the
  URL; nothing recomputes a slug after the fact.

## The one command

```sh
cd server
.venv/bin/python manage.py rename_place <old-slug> <new-slug>
.venv/bin/python manage.py rename_place ojai-2 ojai-california      # example
.venv/bin/python manage.py rename_place ojai-2 ojai-california --dry-run
```

- `<old-slug>` can be **any** slug the place currently answers to — its canonical
  slug or an existing alias. It only has to identify the place.
- `<new-slug>` becomes the new canonical. It must be a valid slug (lowercase,
  hyphens, no spaces) and must not already be taken (see below).
- `--dry-run` prints what would change and writes nothing.

## What a rename actually does — and why old links don't break

The old slug is **kept as an alias** and permanently `301`-redirects to the new
one. So after `rename_place ojai-2 ojai-california`:

- `/place/ojai-california` is the canonical URL (address bar, sitemap, Copy link).
- `/place/ojai-2` still works — it 301s to `/place/ojai-california`.

That means every URL that was already shared, indexed, bookmarked, or
hand-written into an article keeps resolving. A rename is safe to do at any time;
it never strands a link. This is the whole reason the alias table exists — before
it, changing a slug silently 404'd every reference to the old one.

You don't need to hunt down and edit in-article cross-links after a rename: a
stale `/place/ojai-2` link just redirects. Cleaning them up is optional tidying,
never required.

## The one thing it won't do: take a slug from another place

If `<new-slug>` is already held by a **different** place, the command **refuses**
and names the holder:

```
CommandError: 'ojai' is held by Ojai Restaurant (pk=42, slug='ojai'). Reclaiming
a slug from another place is not supported here — see docs/slug-renames.md.
```

This is deliberate. A slug maps to exactly one place, so giving `ojai` to the town
would mean **taking it from the restaurant** — and any existing `/place/ojai` link
that meant the restaurant would then silently point at the town. Repointing a live
URL at a different thing is a judgement call, not an automatic one, so v1 doesn't
do it through this command.

If you genuinely need to reclaim a slug (rare, and usually only for a bare "prime"
name), do it by hand in a shell, knowing you're repointing the old URL:

```python
# server/.venv/bin/python manage.py shell
from core.models import PlaceSlug
from core.slugs import place_by_slug
restaurant = place_by_slug('ojai')
PlaceSlug.objects.filter(place=restaurant, slug='ojai').delete()  # release it
# then give it to the town:
town = place_by_slug('ojai-2')
# ...rename_place town-slug -> ojai now succeeds, since 'ojai' is free
```

## Special case that *is* allowed: promoting your own alias

If `<new-slug>` is a slug **this same place** already holds as an alias, the
command just promotes it to canonical (no new row, no repoint) — e.g. renaming
back to a slug you previously renamed away from.

## After a rename

Nothing else to do. The redirect is live immediately, the sitemap regenerates
with the canonical slug on its next fetch, and the running app doesn't need a
restart. If you renamed a place that already has inbound external links, the 301
tells search engines to move their ranking to the new URL over time.

---

*This doc is the operator procedure; the design rationale — why the alias table
exists, why reclaiming a slug from another place is out of scope — lives in the
project's internal design notes.*
