# Which places are clickable — tuning the POI filter

Toponymia is a wiki about **place names**. A restaurant called OJAI is a
business name, not a toponym, so commercial points of interest are kept out
of the two surfaces that can create an article: the clickable map, and the
search box's geocoder results.

This document is how to change what's allowed. All of it lives in one file,
[`web/src/poi.ts`](../web/src/poi.ts).

## Why it matters more than it looks

A place, once created, takes a URL slug forever — `/place/ojai` goes to
whoever resolved it first, and there is no rename or merge tool yet. Authors
hand-write cross-links as `/place/<slug>`, and the slug is the canonical URL
in the sitemap. So a business that slips through doesn't just add clutter: it
can permanently occupy the clean name of a real town.

## The two lists, and why there are two

The same rule has to be expressed twice, in different vocabularies:

| Surface | Vocabulary | Where |
|---|---|---|
| Map (clickable features) | OpenMapTiles derived `class` / `subclass` — e.g. `castle`, `railway`+`station` | `POI_CLASS_ALLOWLIST`, `RAILWAY_SUBCLASSES` (station only — see the `halt` warning) |
| Search (Photon geocoder) | Raw OSM tags — e.g. `historic=castle`, `railway=station` | `PHOTON_TAG_ALLOWLIST`, `PHOTON_COMMERCIAL_KEYS`, `PHOTON_TRANSIT_DENY` |

They **cannot be shared** — the tile schema and raw OSM tags genuinely
disagree — so they sit next to each other in one file with comments pointing
across. **Change one, change the other**, or the search box will offer
something the map refuses to draw. (That exact mismatch shipped once and had
to be fixed.)

Hiding a feature from the map is also what makes it unclickable:
`queryRenderedFeatures` only returns features from layers present in the
style, so there's no separate click filter to keep in step.

## They are allowlists, deliberately

Only listed categories appear. This is the opposite of the obvious design,
and the reason is the failure mode: OpenStreetMap gains categories over
time, and a denylist silently admits every one nobody has heard of — which
is precisely how a restaurant became a place. Failing closed means a new
category is invisible until someone adds it here. On this project that's the
right direction to be wrong.

## Adding or removing a map category

1. **Find the real class name.** Do not trust the style — see the warning
   below. Decode a tile that contains your feature (recipe below) and read
   its `class` / `subclass`.
2. Add it to `POI_CLASS_ALLOWLIST` in `web/src/poi.ts`.
3. Mirror it in the Photon lists so search agrees.
4. Rebuild and eyeball it (`npm run build`, then a screenshot check).

Verified-real class values not currently shown, if you want them:
`monument`, `museum`, `art_gallery`.

> **Do not read category names off the map style.** The OpenFreeMap Liberty
> style has a `poi_transit` layer filtering on `class in [airport, bus,
> rail]`, but planet tiles carry **`railway`**, not `rail` — so that layer
> draws no station at all, and an allowlist written from it silently matches
> nothing. Always confirm against tile data.

> **Do not trust OSM's documented meaning either.** `railway=halt` is
> documented as a small passenger station — the British "Bearsted Halt"
> sense — and was allowed on that basis. In practice it is used for light
> rail: one central Portland tile carries 24 halts and 23 tram stops, the
> same MAX system tagged both ways, named for the crossings they sit on
> ("Library/Southwest 9th Avenue"). Meanwhile the small-station case the
> name suggests is tagged `station` anyway (Cantley, Norfolk, population
> ~350). Tagging practice beats the wiki page; count real features before
> allowing a category.

## Adding or removing a search category

Photon returns `osm_key` / `osm_value`. Three controls, checked in order by
`isToponymicPhotonHit()`:

- `PHOTON_COMMERCIAL_KEYS` — keys dropped wholesale (`amenity`, `shop`,
  `office`, `craft`, `healthcare`, `emergency`). Every value under them is a
  business or an institution.
- `PHOTON_TRANSIT_DENY` — specific values under otherwise-fine keys: bus
  stops, tram stops, platforms, `railway=halt` (light rail, see above), and
  `railway=stop` (the operational node beside a station, pure duplication —
  three of Photon's four "Cork Kent" rows).
- `PHOTON_TAG_ALLOWLIST` — for *mixed* keys, the only values allowed
  (`historic=castle`, `man_made=lighthouse`, `tourism=attraction`). A key
  that appears here is otherwise fully blocked.

Keys not mentioned anywhere — `place`, `waterway`, `natural`, `boundary`,
`highway`, `railway`, `aeroway`, `landuse`, `leisure` — pass through. They
are the toponymic bulk of the geocoder.

## Same-name collisions

Some features legitimately share a name with the place they sit in —
Paddington the station and Paddington the district. These are **not**
filtered, because the category can't distinguish them from stations with
etymologies of their own (Cork Kent is named for Thomas Kent, executed in
1916). Instead the toponym is ranked first:

- **Search:** `place`-key hits claim their `name|context` dedupe key in a
  pre-pass, so the district always wins the row. A differently-named station
  ("London Paddington") has its own key and still appears.
- **Picker:** `groupByName` / `nameRank` in `web/src/map/features.ts` keeps
  same-named candidates together, ordered article-dots → `place` → the rest.

Clicking the station still opens the *station*, by design — same name does
not mean same entity.

## Verifying a change

**Search, against live Photon.** Check both that the unwanted thing goes and
that a wanted thing stays:

```sh
curl -s 'https://photon.komoot.io/api/?q=Ojai&limit=8&lat=50.08&lon=14.44' \
  | python3 -m json.tool | grep -E '"(name|osm_key|osm_value)"'
```

Useful cases: `Ojai` biased to Prague (a restaurant that must be dropped),
`Paddington` biased to London (eight rows, one toponym), `Cork Kent` and
`Gare Saint-Lazare` (stations that must survive).

**Map, by decoding a real tile.** Find the current tile URL, then read the
`poi` layer's properties:

```sh
curl -s https://tiles.openfreemap.org/planet          # -> tiles[] template
curl -s --compressed \
  "https://tiles.openfreemap.org/planet/<version>/14/8297/5635.pbf" -o t.pbf
```

```js
// npm install @mapbox/vector-tile pbf   — note: the ESM export is
// `PbfReader`, not a default export.
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import fs from 'fs'
const layer = new VectorTile(new PbfReader(fs.readFileSync('t.pbf'))).layers.poi
for (let i = 0; i < layer.length; i++) console.log(layer.feature(i).properties)
```

`14/8297/5635` is central Paris (z14 is the tile maxzoom). For other places,
convert lat/lon with the standard slippy-map formula.

**Visually.** Serve the built app and screenshot a dense area — central
Prague `#17/50.0875/14.4213` for commercial POIs, Paris
`#15/48.8757/2.3255` for stations:

```sh
cd web && npm run build && npm run preview -- --port 4173
chromium-browser --headless=new --enable-unsafe-swiftshader \
  --use-angle=swiftshader --window-size=1000,700 \
  --virtual-time-budget=20000 --screenshot=out.png \
  'http://localhost:4173/#17/50.0875/14.4213'
```

## Known gaps

- **This is prevention, not enforcement.** `feature_class` is a free-form
  string chosen by the client, so a direct `POST /api/resolve/` can still
  create a place of any kind. The rule belongs in the server's `resolve()`
  to be a rule; until then these lists only govern the UI.
- `landuse=retail` and `building=*` still pass search. Neither is a
  collision risk — both carry distinct names — but neither is really a
  toponym either.
