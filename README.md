# Toponymia

This is the source code for **[www.toponymia.org](https://www.toponymia.org)**.

> **toponym** — a place name; a word derived from the name of a place.

Toponymia is a map-based wiki about the origins and meanings of place
names: a full-page map where towns, rivers, roads, and mountains link
to articles about how they got their names.

## How it works

- **The map is the index.** No markers. Every named feature on the map
  — a town label, a river line, a road — is clickable. Clicking opens
  the article for that place in a side pane (or a stub inviting you to
  write it). If several features overlap at the click point, a small
  picker lets you choose. Anyone can open a place Toponymia already
  knows; identifying one for the *first* time queries OpenStreetMap's
  public Overpass service under our own IP and creates a permanent
  record, so that step asks you to sign in.
- **Highlights instead of pins.** Places that have articles are shown
  by recoloring the basemap's own labels amber, so every rendered
  instance of a name lights up (a river along its whole course) and
  visibility follows the map's own zoom and collision choices. An "All
  articles" toggle adds a dot for any article whose label isn't
  currently drawn. Signed in, "Map my contributions" in the account
  panel turns the same map into a view of everywhere you've written or
  discussed, framed on your own footprint.
- **Wanted places get a quieter highlight.** A discussion can start
  before anyone writes the article, so a place with talk and no article
  lights up too — a much darker amber label, and a hollow ring rather
  than a filled dot. Outline instead of fill, so the mark reads as a
  lesser claim than an article and clicking it lands you somewhere
  honest: the pane opens on the discussion, not on an empty article.
  The Talk tab carries its thread count either way, so a written place
  with an argument running under it doesn't look settled.
- **English-first labels, one article per place.** Labels, the picker,
  and article titles resolve to an English name where one exists, so
  Greenland reads "Greenland", not "Kalaallit Nunaat" — while the
  native name gets its own etymology section inside the one article. A
  header dropdown can relabel the whole map in another language as a
  browsing lens.
- **Stable identity.** Articles are anchored to real-world entities,
  not coordinates: a Wikidata ID when one exists, an OpenStreetMap
  element otherwise, a name + location as a last resort. One article
  covers the whole river, no matter how many map segments it renders
  as. Clicked tile features are resolved server-side via the Overpass
  API.
- **A true wiki.** Accounts, full revision history, side-by-side
  diffs, revert, and threaded talk pages. Talk attaches to the *place*
  rather than the article, so you can argue about a name before anyone
  has written a word about it. Etymology is written per *name* — a
  place's endonym and its exonyms each get their own etymology section
  within one article, with language fields validated as ISO 639-3
  codes.
- **Structured etymology, not just prose.** A name can carry more than
  one etymology, because contested names are the normal case: each
  hypothesis has its own sources, its own cited languages, and an
  optional label saying how well established it is — including *folk
  etymology*, so a traditional story can be recorded as one without
  being asserted as true. Each also takes an optional word breakdown
  (the etymon, its language, its meaning, its role), which is what makes
  an etymology searchable rather than only readable. The prose box stays
  the floor: everything structured is optional and folded away until
  someone wants it.
- **Community tools.** Report affordances on revisions and talk posts,
  a moderator queue, soft-delete, per-article protection levels, and
  rate limits on the expensive endpoints. Filing a report emails the
  moderators — plain SMTP, no transactional API key, BCC'd, and capped
  at 20 an hour so a burst can't flood an inbox. In development that
  goes to the console with everything else.

## Stack

- **Backend:** Django 6 + Django REST Framework, PostgreSQL + PostGIS
  (GeoDjango).  Auth via django-allauth in headless mode.
- **Frontend:** React 19 + TypeScript + Vite, MapLibre GL JS.
- **Map data:** OpenStreetMap vector tiles via OpenFreeMap (hosted,
  keyless); Overpass for resolving clicked features to OSM elements,
  Wikidata IDs for stable anchoring, Photon for geocoding search.

No API keys anywhere — every external dependency is free and open.

## Repository layout

```
server/   Django project (config/) + the core app (models, API, SPA serving)
web/      Vite + React + TypeScript frontend (MapLibre map, article pane)
docs/     Maintainer notes for things that need tuning over time
docker-compose.yml   PostGIS for local development
```

- [`docs/poi-filtering.md`](docs/poi-filtering.md) — which features are
  clickable and searchable, and how to change the lists.
- [`docs/slug-renames.md`](docs/slug-renames.md) — how to rename a place's
  URL slug without breaking existing links.

In production Django serves the built frontend from `web/dist` (SPA
plus server-rendered SEO meta, sitemap, and robots); in development
the two run separately and Vite proxies API calls to Django.

## Development

### Prerequisites

- **Docker** (for the PostGIS database) — or a local PostgreSQL 16 +
  PostGIS 3.4.
- **Python 3.12+** (the project is developed on 3.14; Django 6
  requires 3.12+).
- **System GDAL/GEOS** for GeoDjango:
  - Fedora: `sudo dnf install gdal geos`
  - Debian/Ubuntu: `sudo apt install gdal-bin libgdal-dev libgeos-dev`
- **Node 22+**.

### Database

```sh
docker compose up -d db
```

Starts PostGIS on `localhost:5432` with database/user/password all
`toponymia` (the defaults the server settings expect).

### Backend

```sh
cd server
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver          # serves the API on :8000
```

Create an account to edit; make a moderator/admin with
`.venv/bin/python manage.py createsuperuser` (moderators are
`is_staff`, admins `is_superuser`).

### Frontend

```sh
cd web
npm install
npm run dev                                    # Vite dev server on :5173
```

The dev and preview servers proxy `/api` and `/_allauth` to
`localhost:8000`, so keep `runserver` up for resolution, articles, and
auth to work.

### Tests and linting

```sh
# server
cd server
.venv/bin/python manage.py test
.venv/bin/ruff check .

# web
cd web
npm run lint                                   # oxlint
npm run build                                  # tsc typecheck + Vite build
```

CI (`.github/workflows/ci.yml`) runs the same checks on push and PR
against a PostGIS service container.

### Production build

```sh
cd web && npm run build                        # emits web/dist
cd ../server && .venv/bin/python manage.py collectstatic --noinput
DJANGO_DEBUG=0 DJANGO_SECRET_KEY=... DJANGO_ALLOWED_HOSTS=... \
  .venv/bin/gunicorn config.wsgi:application
```

**`DEBUG` is off by default**, so a deployment that forgets a variable
gets a hardened server rather than tracebacks on every error page —
and, because the `SECRET_KEY` check runs only when `DEBUG` is off, a
misconfigured deploy fails loudly at startup instead of serving. The
development commands above need no setup: `manage.py` turns `DEBUG`
back on, and gunicorn (which imports `config.wsgi`) never goes through
it.

With `DEBUG` off, Django serves the built SPA (WhiteNoise for assets),
turns on secure cookies, and expects TLS + `X-Forwarded-Proto` from a
reverse proxy. See `server/config/settings.py` for the full
environment-variable contract.

### Error logging

Unhandled exceptions go to **stderr unconditionally**, so a deployment
that configures nothing still records its own 500s wherever gunicorn
sends its error log. This is a deliberate replacement for Django's
default, which routes request errors to a console handler filtered on
`DEBUG` being *on* and to `mail_admins` — meaning that with `DEBUG` off
and no `ADMINS`, tracebacks are reported nowhere at all.

Set `DJANGO_LOG_DIR` to also write them to `toponymia.log` in that
directory, rotating at 10 MB with 4 backups (a 50 MB ceiling, because
logs share the root volume with the database). `DJANGO_LOG_LEVEL`
tunes the application logger only; request and security errors are
always at `ERROR`.

### Content-Security-Policy

`SECURE_CSP` in `server/config/settings.py` is the whole policy, sent by
Django's built-in CSP middleware. Two things about it are easy to trip
over:

- **Inline scripts need the request's nonce.** `core/spa.py` stamps the
  ones in `index.html` automatically as it renders the shell; a new
  inline script added anywhere else is silently dropped by the browser.
- **`npm run dev` does not exercise it.** Vite serves the SPA in
  development, so the policy only applies once Django is serving the
  build. To check it locally:

  ```sh
  cd web && npm run build
  cd ../server && DJANGO_DEBUG=0 DJANGO_SECRET_KEY=any-long-random-string \
    DJANGO_ALLOWED_HOSTS=127.0.0.1 \
    .venv/bin/python manage.py runserver 127.0.0.1:8099
  ```

  Then open the site with the browser console visible and pan, zoom,
  search, and open an article. A too-strict policy breaks the map in
  ways that look like unrelated bugs, so the console is the real test.

The policy allows exactly two external origins — `tiles.openfreemap.org`
for the basemap and `photon.komoot.io` for geocoding. If either moves,
the setting has to move with it. Note that images in article Markdown
are restricted to the site's own origin: hotlinked external images will
not load.

## License

Toponymia licenses its software and its content separately:

- **Software** (`server/`, `web/`) — [GNU Affero GPL v3](LICENSE).
- **Wiki content** (articles, etymology, talk) — [CC BY-SA
  4.0](LICENSE-CONTENT.md); contribution terms are in
  [TERMS.md](TERMS.md).

Map data © OpenStreetMap contributors (ODbL); basemap tiles by
OpenFreeMap; identifiers and labels from Wikidata (CC0). See
[LICENSE-CONTENT.md](LICENSE-CONTENT.md).
