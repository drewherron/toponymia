# Which places are clickable — tuning the POI filter

Toponymia is a wiki about **place names**. A restaurant called OJAI is a
business name, not a toponym, so commercial points of interest are kept out
of the two surfaces that can create an article: the clickable map, and the
search box's geocoder results.

This document is how to change what's allowed. All of it lives in one file,
[`web/src/poi.ts`](../web/src/poi.ts).

## Why it matters more than it looks

A place, once created, takes a URL slug from whoever resolved it first —
`/place/ojai`. It can be changed after the fact ([`docs/slug-renames.md`](slug-renames.md)),
but only to a *free* slug, and the rename leaves the old one as a redirect; you
can't cleanly hand a business's `ojai` to the real town. Authors hand-write
cross-links as `/place/<slug>`, and the slug is the canonical URL in the
sitemap. So a business that slips through doesn't just add clutter: it can
occupy the clean name of a real town, and getting it back is not tidy.

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

See [the class reference](#reference-the-class-vocabulary) below for what
exists and what's worth considering.

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

## Reference: the class vocabulary

Counts below are from **eight z14 tiles** — central Paris, Portland, London,
rural Norfolk, Tokyo, New York, Cairo and Sydney — totalling 32,552 POI
features in **94 distinct classes**. They show relative volume, not global
truth; regenerate them with the script at the end of this section.

**Allowed today**

| class | in sample | note |
|---|---|---|
| `castle` | 19 | subclasses `castle`, `ruins` |
| `attraction` | 163 | subclasses `attraction`, `viewpoint` |
| `railway` | 142 | **`subclass=station` only** — see the `halt` warning |
| `lighthouse` | 0 | coastal, so absent from these eight urban tiles; it is a real class (it appears in the style's sprite sheet) |

**Plausible additions** — things that carry a name of their own, roughly in
order of how comfortably they read as toponyms:

| class | in sample | caution |
|---|---|---|
| `monument` | 42 | |
| `museum` | 78 | |
| `castle` | — | already allowed |
| `harbor` | 24 | subclass `marina` |
| `cemetery` | 45 | subclasses `grave_yard`, `cemetery` |
| `ferry_terminal` | 54 | |
| `stadium` | 4 | often a sponsor's name, which is a business name again |
| `zoo` / `aquarium` | 1 / 3 | |
| `town_hall` | 96 | subclasses include `courthouse`, `public_building` |
| `place_of_worship` | 200 | genuinely toponymic (Notre-Dame, Hagia Sophia) but the class is *every* church, most of them unremarkable |
| `college` | 66 | includes `university` |
| `art_gallery` | 1007 | **mostly `subclass=artwork`** — street art, not galleries. Narrow by subclass if you want this |
| `park` / `garden` | 407 / 2667 | huge volume, and parks already reach the map through their own `park` source layer |

**Everything else** is commercial premises, civic amenities, or street
furniture, and should stay out. In descending volume:
`bicycle_parking`, `shop`, `restaurant`, `waste_basket`, `gate`, `bollard`,
`office`, `parking`, `cafe`, `bus`, `fast_food`, `information`,
`clothing_store`, `lodging`, `entrance`, `bar`, `post`, `drinking_water`,
`toilets`, `pitch`, `hairdresser`, `bicycle_rental`, `telephone`, `bank`,
`shelter`, `beer`, `recycling`, `school`, `grocery`, `motorcycle_parking`,
`pharmacy`, `atm`, `swimming_pool`, `bakery`, `library`, `lift_gate`,
`theatre`, `playground`, `alcohol_shop`, `brownfield`, `laundry`,
`hospital`, `doctors`, `dentist`, `fuel`, `ice_cream`, `music`, `car`,
`sports_centre`, `police`, `cinema`, `chess`, `bicycle`, `butcher`,
`cycle_barrier`, `fire_station`, `veterinary`, `dog_park`, `running`,
`athletics`, `picnic_site`, `golf`, `prison`, `basin`, `swimming`,
`escape_game`, `campsite`, `climbing`, `hackerspace`, `boxing`, `ice_rink`,
`cycling`, `yoga`, `billiards`, `skateboard`, `gymnastics`, `toll_booth`.

### On `subclass`

`subclass` is the **raw OSM tag value**, so it is open-ended and cannot be
enumerated — new values appear whenever mappers use a new tag. Filter on
`class` by default, and reach for `subclass` only to *narrow* an allowed
class, the way `railway` is narrowed to `station`.

### Regenerating these numbers

```js
// npm install @mapbox/vector-tile pbf
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import fs from 'fs'
const tally = {}, subs = {}
for (const f of process.argv.slice(2)) {
  const l = new VectorTile(new PbfReader(fs.readFileSync(f))).layers.poi
  for (let i = 0; i < (l?.length ?? 0); i++) {
    const p = l.feature(i).properties
    tally[p.class] = (tally[p.class] ?? 0) + 1
    ;(subs[p.class] ??= new Set()).add(p.subclass)
  }
}
for (const [c, n] of Object.entries(tally).sort((a, b) => b[1] - a[1]))
  console.log(String(n).padStart(6), c.padEnd(20), [...subs[c]].slice(0, 5).join(','))
```

Fetch tiles with the recipe in the previous section; convert lat/lon to
`z/x/y` with the standard slippy-map formula.

## Known gaps

- **This is prevention, not enforcement.** `feature_class` is a free-form
  string chosen by the client, so a direct `POST /api/resolve/` can still
  create a place of any kind. The rule belongs in the server's `resolve()`
  to be a rule; until then these lists only govern the UI.
- `landuse=retail` and `building=*` still pass search. Neither is a
  collision risk — both carry distinct names — but neither is really a
  toponym either.
