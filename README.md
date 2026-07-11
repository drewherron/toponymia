# Toponymia

> **toponym** — a place name; a word derived from the name of a place.

Toponymia is a map-based wiki about the origins and meanings of place names: a
full-page map where towns, rivers, roads, and mountains link to articles about
how they got their names.

## Status

**Under reconstruction.** The project is being rewritten from (almost) scratch;
the old prototype has been removed and the new stack is going in piece by piece.

## How it will work

- **The map is the index.** No markers. Every named feature on the map — a town
  label, a river line, a road — is clickable. Clicking opens the article for
  that place in a side pane (or a stub inviting you to write it). If several
  features overlap at the click point, a small picker lets you choose.
- **Highlights instead of pins.** Places that have articles are painted on the
  map itself: tinted lines for rivers and roads, highlighted labels for towns.
  A filter can limit the map to places with articles.
- **Stable identity.** Articles are anchored to real-world entities, not
  coordinates: a Wikidata ID when one exists, an OpenStreetMap element
  otherwise, a name + location as a last resort. One article covers the whole
  river, no matter how many map segments it renders as.
- **A true wiki.** Accounts, revision history, diffs, and talk pages. Etymology
  is written per *name* — a place's endonym and its exonyms each get their own
  etymology section within one article, so names can be filtered and queried by
  language.

## Stack

- **Backend:** Django + Django REST Framework, PostgreSQL + PostGIS
- **Frontend:** React + TypeScript + Vite, MapLibre GL JS
- **Map data:** OpenStreetMap vector tiles via OpenFreeMap; Overpass and
  Wikidata for resolving clicked features to stable entities
