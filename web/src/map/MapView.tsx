import type { FeatureCollection } from 'geojson'
// Relative path: the package's `exports` map hides its dist build, but
// MapLibre's plugin loader needs the dist UMD file, served as an asset.
import rtlTextUrl from '../../node_modules/@mapbox/mapbox-gl-rtl-text/dist/mapbox-gl-rtl-text.js?url'
import maplibregl from 'maplibre-gl'
import type { ExpressionSpecification } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useEffect, useRef } from 'react'
import type { RefObject } from 'react'
import { fetchHighlights, fetchPlaceGeometry } from '../api'
import {
  COARSE_QUERY,
  PANE_MAX_VW,
  PANE_WIDTH,
  sheetHeight,
  type SheetDetent,
} from '../layout'
import type {
  ClickContext,
  FeatureCandidate,
  GeocodeHit,
  MapApi,
  MapPadding,
  ResolvedPlace,
} from '../types'
import { poiClassFilter } from '../poi'
import { kindOf, labelClassExpr, toCandidates } from './features'
import { nameField, nameKeys, tokenMatches } from './labels'

const MAP_STYLE = 'https://tiles.openfreemap.org/styles/liberty'
const CLICK_TOLERANCE_PX = 6
// A fingertip covers ~44px, so a mouse-sized query box means most taps on a
// phone hit nothing — and "every named feature is clickable" is the whole
// product. Applied under (pointer: coarse) only.
const TOUCH_TOLERANCE_PX = 22
// Framing a place gets flown to — shared by flyToPlace and the "home view"
// the recenter button compares against, so the two never disagree.
const FIT_PADDING = 80
const FIT_MAXZOOM = 14
const FLY_ZOOM = 12
// Zoom for a place with no cached footprint, by feature class. A flat
// FLY_ZOOM is a street-level number: fine for the villages and POIs that
// usually lack a bbox, absurd for a country. Countries reach this path
// because an antimeridian-crossing extent has no planar bbox at all
// (`bounds_of` in overpass.py), so France arrives here with nothing but a
// point on the mainland — z4 frames that as a country instead of a
// Parisian street. Keys are the tile/Photon `class` values; anything
// unlisted keeps FLY_ZOOM.
const FLY_ZOOM_BY_CLASS: Record<string, number> = {
  continent: 3,
  country: 4,
  state: 5,
  province: 5,
  region: 5,
  county: 7,
  island: 7,
  city: 10,
  town: 11,
  village: 12,
  hamlet: 13,
  suburb: 13,
  neighbourhood: 14,
}

function flyZoomFor(place: ResolvedPlace): number {
  return FLY_ZOOM_BY_CLASS[place.feature_class] ?? FLY_ZOOM
}
// How far the live camera may drift from the home view and still count as
// "there" — a few screen px of pan and a hair of zoom, so the fly landing's
// rounding isn't read as movement while any real pan/zoom is.
const HOME_PAN_PX = 6
const HOME_ZOOM_EPS = 0.05
// A point has no extent, so it's framed as a tiny box: one code path for both
// branches means the padding offset below is applied identically. Non-zero to
// keep the fit's division off 0/0 — the box is far under a pixel at any zoom,
// so maxZoom always wins and the fit is a pure centre-on-point.
const POINT_BOUNDS_EPS = 1e-6
// Never pad the camera down to nothing: a full-height sheet plus FIT_PADDING
// exceeds the canvas, and a fit into negative space is meaningless.
const MIN_VISIBLE_PX = 60

const NO_CHROME: MapPadding = { top: 0, right: 0, bottom: 0, left: 0 }

/** Shrink an opposing padding pair to fit, keeping their ratio. */
function clampPair(a: number, b: number, total: number): [number, number] {
  const room = Math.max(total - MIN_VISIBLE_PX, 0)
  if (a + b <= room) return [a, b]
  if (a + b === 0) return [0, 0]
  const scale = room / (a + b)
  return [a * scale, b * scale]
}

/** How much of the map each edge's chrome covers. The pane overlays the
 *  canvas, so anything the camera aims at must land in what's left. (The
 *  diff view's wider pane is ignored: you're reading a diff, not looking at
 *  the map.) */
function chromeFor(
  map: maplibregl.Map,
  narrow: boolean,
  paneOpen: boolean,
  detent: SheetDetent,
): MapPadding {
  if (!paneOpen) return NO_CHROME
  const canvas = map.getCanvas()
  if (narrow) {
    return { ...NO_CHROME, bottom: sheetHeight(detent, canvas.clientHeight) }
  }
  return {
    ...NO_CHROME,
    left: Math.min(PANE_WIDTH, canvas.clientWidth * PANE_MAX_VW),
  }
}

/** Breathing room + the chrome, clamped to leave a usable window. */
function fitPadding(map: maplibregl.Map, chrome: MapPadding): MapPadding {
  const canvas = map.getCanvas()
  const [left, right] = clampPair(
    FIT_PADDING + chrome.left,
    FIT_PADDING + chrome.right,
    canvas.clientWidth,
  )
  const [top, bottom] = clampPair(
    FIT_PADDING + chrome.top,
    FIT_PADDING + chrome.bottom,
    canvas.clientHeight,
  )
  return { top, right, bottom, left }
}

/** The place as a box to fit: its footprint, else a speck at the label point. */
function placeBounds(place: ResolvedPlace): [[number, number], [number, number]] {
  if (place.bbox) {
    const [w, s, e, n] = place.bbox
    return [
      [w, s],
      [e, n],
    ]
  }
  const [lng, lat] = place.label_point ?? place.centroid
  return [
    [lng - POINT_BOUNDS_EPS, lat - POINT_BOUNDS_EPS],
    [lng + POINT_BOUNDS_EPS, lat + POINT_BOUNDS_EPS],
  ]
}

/** The camera flyToPlace lands on: fit the bbox when we have one, else
 *  center on the label point at a default zoom. cameraForBounds computes the
 *  fit without moving the map, so it matches fitBounds exactly — including
 *  the way asymmetric padding shifts the centre off the box's own middle. */
function homeCamera(
  map: maplibregl.Map,
  place: ResolvedPlace,
  chrome: MapPadding,
): { center: { lng: number; lat: number }; zoom: number } {
  const cam = map.cameraForBounds(placeBounds(place), {
    padding: fitPadding(map, chrome),
    maxZoom: place.bbox ? FIT_MAXZOOM : flyZoomFor(place),
  })
  if (cam?.center && cam.zoom != null && Number.isFinite(cam.zoom)) {
    const center = maplibregl.LngLat.convert(cam.center)
    return { center: { lng: center.lng, lat: center.lat }, zoom: cam.zoom }
  }
  const [lng, lat] = place.label_point ?? place.centroid
  return {
    center: { lng, lat },
    zoom: place.bbox ? FIT_MAXZOOM : flyZoomFor(place),
  }
}

// Shapes Arabic/Hebrew label text (self-hosted; lazy = fetched only when
// RTL text first appears in view). Without it RTL names render backward.
maplibregl.setRTLTextPlugin(rtlTextUrl, true).catch(console.error)

// Darker amber for label text (readability at small sizes), brighter for dots.
const LABEL_COLOR = '#b45309'
const DOT_COLOR = '#d97706'

const EMPTY_COLLECTION: FeatureCollection = {
  type: 'FeatureCollection',
  features: [],
}

// The OpenMapTiles vector source (see the style's `sources`). `place`
// labels carry stable OSM-derived feature ids there, so feature-state
// keyed by id survives tile reloads — the basis of the tier-2 highlight.
const OMT_SOURCE = 'openmaptiles'
const SPATIAL_SOURCE_LAYER = 'place'
// A rendered label counts as an article's own only if it sits within this
// of the article's label_point (~5.5 km). Far enough to absorb a city
// node vs its P625 centre, tight enough to reject a same-named city in
// another state (Columbia SC vs DC, the Franklins — all ≥300 km apart).
const SPATIAL_MATCH_DEG = 0.05

/** Recolor expression for a `place`-layer label: amber iff feature-state
 *  marks it an article (set by the spatial reconciler). */
function spatialColor(originalColor: unknown): ExpressionSpecification {
  return [
    'case',
    ['boolean', ['feature-state', 'article'], false],
    LABEL_COLOR,
    originalColor,
  ] as ExpressionSpecification
}

interface LabelLayer {
  id: string
  originalColor: unknown
  /** Class part of this layer's highlight tokens. */
  classExpr: ExpressionSpecification | string
  /** `place`-layer labels are recolored by feature-state, set from a
   *  spatial match (tier 2), not the name|class token expression — so two
   *  same-named cities can be told apart. Everything else uses tokens. */
  spatial: boolean
}

interface MapViewProps {
  onClickFeatures: (
    candidates: FeatureCandidate[],
    point: { x: number; y: number },
    click: ClickContext,
  ) => void
  onMoveStart: () => void
  /** Fires after the map settles (moveend) and once on load, so callers
   *  can re-test whether the selected place is still in view. */
  onViewportChange: () => void
  /** Show dots for articles whose basemap label isn't rendered here. */
  allArticles: boolean
  /** Language the basemap labels render in (labels.ts codes). */
  labelLanguage: string
  /** Bump to force a highlight refetch (e.g. after saving an article). */
  highlightsEpoch: number
  /** Sheet mode: the pane covers the bottom rather than the left. */
  narrow: boolean
  /** The pane is open, so it's covering part of the canvas. */
  paneOpen: boolean
  /** Settled sheet detent — never the live drag offset, or the home view
   *  (and with it the recenter button) would churn mid-gesture. */
  sheetDetent: SheetDetent
  /** Receives imperative controls (fly-to for search/deep links). */
  mapApi: RefObject<MapApi | null>
}

function MapView({
  onClickFeatures,
  onMoveStart,
  onViewportChange,
  allArticles,
  labelLanguage,
  highlightsEpoch,
  narrow,
  paneOpen,
  sheetDetent,
  mapApi,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
  const readyRef = useRef(false)
  const abortRef = useRef<AbortController | null>(null)
  const propsRef = useRef({
    onClickFeatures,
    onMoveStart,
    onViewportChange,
    allArticles,
    labelLanguage,
    narrow,
    paneOpen,
    sheetDetent,
  })
  const labelLayersRef = useRef<LabelLayer[]>([])
  const articleTokensRef = useRef<string[]>([])
  const collectionRef = useRef<FeatureCollection>(EMPTY_COLLECTION)
  const hiddenDotsRef = useRef('')
  // Place-layer feature ids we've marked amber via feature-state (tier 2),
  // so the reconciler knows what to clear when a label stops matching.
  const litPlaceIdsRef = useRef<Set<string | number>>(new Set())
  const focusAbortRef = useRef<AbortController | null>(null)
  // Slug of the place currently drawing its course — set only once
  // geometry is actually on the map, so a city (which has none) keeps its
  // dot. Read by updateDotFilter.
  const focusSlugRef = useRef<string | null>(null)
  // Down once the programmatic fly that requested the highlight has
  // settled; until then a move is ours, not the user's. See the teardown
  // in the 'movestart' handler.
  const focusFlyingRef = useRef(false)

  useEffect(() => {
    propsRef.current = {
      onClickFeatures,
      onMoveStart,
      onViewportChange,
      allArticles,
      labelLanguage,
      narrow,
      paneOpen,
      sheetDetent,
    }
  }, [
    onClickFeatures,
    onMoveStart,
    onViewportChange,
    allArticles,
    labelLanguage,
    narrow,
    paneOpen,
    sheetDetent,
  ])

  /** Wrap every label layer's text color: article labels go amber.
   *  Matches `name|class` tokens against the displayed name, the raw
   *  `name`, and the English name (article names are English by
   *  construction), so places light up whatever the label language —
   *  while the class gate keeps a same-named feature of another kind
   *  (the country "Mexico" vs the city's article) dark. */
  const applyLabelColors = (map: maplibregl.Map) => {
    const tokens = articleTokensRef.current
    const lang = propsRef.current.labelLanguage
    for (const { id, originalColor, classExpr, spatial } of labelLayersRef.current) {
      // Spatial (place-layer) labels are colored by feature-state, set once
      // at load and driven by reconcilePlaceHighlights — skip them here.
      if (spatial) continue
      map.setPaintProperty(id, 'text-color', [
        'case',
        tokenMatches(lang, classExpr, tokens),
        LABEL_COLOR,
        originalColor,
      ])
    }
  }

  /**
   * Tier 2: decide which *place*-layer labels are articles by
   * position, not just name+class — so Columbia SC stays dark while the
   * "Columbia" name on Washington D.C. lights only D.C.'s own label. For
   * each rendered place label, match its name|class token to an article and
   * require the label to sit within SPATIAL_MATCH_DEG of that article's
   * label_point; mark the hits amber via feature-state (which persists
   * across tile reloads, keyed by the tile's stable OSM feature id).
   */
  const reconcilePlaceHighlights = (map: maplibregl.Map) => {
    const spatialLayers = labelLayersRef.current
      .filter((layer) => layer.spatial)
      .map((layer) => layer.id)
    if (spatialLayers.length === 0) return
    // token → the label_points of the articles bearing it.
    const tokenPoints = new Map<string, [number, number][]>()
    for (const feature of collectionRef.current.features) {
      const props = feature.properties as {
        names?: string[]
        feature_class?: string
      } | null
      if (feature.geometry?.type !== 'Point') continue
      const point = feature.geometry.coordinates as [number, number]
      const cls = props?.feature_class ?? ''
      for (const name of props?.names ?? []) {
        const token = `${name}|${cls}`
        const points = tokenPoints.get(token)
        if (points) points.push(point)
        else tokenPoints.set(token, [point])
      }
    }
    const keys = [
      ...new Set([...nameKeys(propsRef.current.labelLanguage), ...nameKeys('en')]),
    ]
    const near = (a: [number, number], b: [number, number]) =>
      Math.abs(a[0] - b[0]) <= SPATIAL_MATCH_DEG &&
      Math.abs(a[1] - b[1]) <= SPATIAL_MATCH_DEG
    const matched = new Set<string | number>()
    const rendered = new Set<string | number>()
    for (const feature of map.queryRenderedFeatures({ layers: spatialLayers })) {
      const id = feature.id
      if (id == null || feature.geometry?.type !== 'Point') continue
      rendered.add(id)
      const at = feature.geometry.coordinates as [number, number]
      const kind = kindOf(feature)
      const props = feature.properties ?? {}
      for (const key of keys) {
        const value = props[key]
        if (typeof value !== 'string' || !value) continue
        const points = tokenPoints.get(`${value}|${kind}`)
        if (points?.some((point) => near(point, at))) {
          matched.add(id)
          break
        }
      }
    }
    const lit = litPlaceIdsRef.current
    for (const id of matched) {
      if (lit.has(id)) continue
      map.setFeatureState(
        { source: OMT_SOURCE, sourceLayer: SPATIAL_SOURCE_LAYER, id },
        { article: true },
      )
      lit.add(id)
    }
    // Clear a previously-lit label only once it's actually on screen and no
    // longer matching — off-screen ids keep their state (harmless) and are
    // re-checked when they render again, so panning away doesn't flicker.
    for (const id of rendered) {
      if (lit.has(id) && !matched.has(id)) {
        map.setFeatureState(
          { source: OMT_SOURCE, sourceLayer: SPATIAL_SOURCE_LAYER, id },
          { article: false },
        )
        lit.delete(id)
      }
    }
  }

  /**
   * A dot only stands in for a missing label: hide it as soon as any of
   * the place's names is currently rendered as a basemap label — or as
   * soon as the feature is drawing its own course, which says where the
   * thing is far better than a dot on it. Runs on 'idle', so the
   * dot→label handoff follows tile/collision changes.
   */
  const updateDotFilter = (map: maplibregl.Map) => {
    const layerIds = labelLayersRef.current.map((layer) => layer.id)
    // `name|class` tokens, same gate as the recolor: a dot hands off to a
    // label only when a same-*kind* label is actually drawn, so the
    // country "Mexico" label no longer hides the city's dot.
    const renderedTokens = new Set<string>()
    const keys = [
      ...new Set([...nameKeys(propsRef.current.labelLanguage), ...nameKeys('en')]),
    ]
    for (const feature of map.queryRenderedFeatures({ layers: layerIds })) {
      const kind = kindOf(feature)
      const props = feature.properties ?? {}
      for (const key of keys) {
        const value = props[key]
        if (typeof value === 'string' && value) {
          renderedTokens.add(`${value}|${kind}`)
        }
      }
    }
    const hidden: string[] = []
    for (const feature of collectionRef.current.features) {
      const props = feature.properties as {
        slug?: string
        names?: string[]
        feature_class?: string
      } | null
      if (!props?.slug) continue
      const cls = props.feature_class ?? ''
      if ((props.names ?? []).some((name) => renderedTokens.has(`${name}|${cls}`))) {
        hidden.push(props.slug)
      }
    }
    // The highlighted feature's own dot, which the label rule misses: at
    // the zoom that frames a whole river there is no label to hand off to.
    const focus = focusSlugRef.current
    if (focus && !hidden.includes(focus)) hidden.push(focus)
    const key = hidden.sort().join('|')
    if (key === hiddenDotsRef.current) return
    hiddenDotsRef.current = key
    map.setFilter('article-dots', [
      '!',
      ['in', ['get', 'slug'], ['literal', hidden]],
    ])
  }

  const refreshHighlights = (map: maplibregl.Map) => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const bounds = map.getBounds()
    fetchHighlights(
      [
        bounds.getWest(),
        bounds.getSouth(),
        bounds.getEast(),
        bounds.getNorth(),
      ],
      controller.signal,
    )
      .then((collection) => {
        collectionRef.current = collection
        // `name|class` tokens: an article lights a label only when both
        // agree, so the country "Mexico" stays dark next to the city's
        // "Mexico" article.
        const tokens = new Set<string>()
        for (const feature of collection.features) {
          const props = feature.properties as {
            names?: string[]
            feature_class?: string
          } | null
          const cls = props?.feature_class ?? ''
          for (const name of props?.names ?? []) tokens.add(`${name}|${cls}`)
        }
        articleTokensRef.current = [...tokens]
        const source = map.getSource('highlights') as
          | maplibregl.GeoJSONSource
          | undefined
        source?.setData(collection)
        applyLabelColors(map)
        // the paint change re-renders; 'idle' then refreshes the dot filter
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) console.error(error)
      })
  }

  useEffect(() => {
    if (!containerRef.current) return

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: MAP_STYLE,
      center: [10, 35],
      zoom: 2,
      hash: true,
    })
    mapRef.current = map
    map.addControl(new maplibregl.NavigationControl(), 'top-right')

    // A camera move issued before the style loads gets dropped, so a fly
    // requested at boot (deep link) waits for 'load'. The gate is our own
    // ready latch, not map.loaded() — loaded() is false whenever the map is
    // busy (tiles streaming, camera animating), which would shelve a fly
    // requested mid-animation instead of letting it interrupt.
    let pendingFly: (() => void) | null = null
    const flyWhenReady = (fly: () => void) => {
      if (readyRef.current) fly()
      else pendingFly = fly
    }
    /** Current chrome, read fresh: the pane may have opened or the sheet
     *  settled at a new detent since the last camera move. */
    const chrome = () => {
      const { narrow, paneOpen, sheetDetent } = propsRef.current
      return chromeFor(map, narrow, paneOpen, sheetDetent)
    }
    const fitBox = (
      bounds: [[number, number], [number, number]],
      maxZoom: number,
      animate = true,
    ) => {
      map.fitBounds(bounds, {
        padding: fitPadding(map, chrome()),
        maxZoom,
        animate,
      })
    }
    const setFocusData = (data: FeatureCollection) => {
      const source = map.getSource('focus-geometry') as
        | maplibregl.GeoJSONSource
        | undefined
      source?.setData(data)
    }
    const clearFocus = () => {
      focusAbortRef.current?.abort()
      focusAbortRef.current = null
      focusFlyingRef.current = false
      const wasFocused = focusSlugRef.current !== null
      focusSlugRef.current = null
      if (readyRef.current) {
        setFocusData(EMPTY_COLLECTION)
        // Give the dot back straight away rather than waiting for 'idle'.
        if (wasFocused) updateDotFilter(map)
      }
    }
    mapApi.current = {
      showFocusGeometry: (slug: string) => {
        // Latch before the caller's fly starts, so the movestart it
        // provokes isn't mistaken for the user moving away.
        clearFocus()
        focusFlyingRef.current = true
        const controller = new AbortController()
        focusAbortRef.current = controller
        fetchPlaceGeometry(slug, controller.signal)
          .then((geometry) => {
            // A second press (or a teardown) landed while we were in
            // flight; that request owns the layer now.
            if (focusAbortRef.current !== controller || !readyRef.current) {
              return
            }
            // Null for area relations — cities simply don't highlight.
            if (!geometry || geometry.type === 'Point') return
            setFocusData({
              type: 'FeatureCollection',
              features: [{ type: 'Feature', geometry, properties: {} }],
            })
            // The course now stands in for the dot the way a label would.
            focusSlugRef.current = slug
            updateDotFilter(map)
          })
          .catch((error: unknown) => {
            if (!controller.signal.aborted) console.error(error)
          })
      },
      clearFocusGeometry: clearFocus,
      flyToPlace: (place: ResolvedPlace, animate = true) => {
        flyWhenReady(() => {
          fitBox(
            placeBounds(place),
            place.bbox ? FIT_MAXZOOM : flyZoomFor(place),
            animate,
          )
        })
      },
      flyToHit: (hit: GeocodeHit) => {
        flyWhenReady(() => {
          if (hit.extent) {
            const [w, s, e, n] = hit.extent
            fitBox(
              [
                [w, s],
                [e, n],
              ],
              FIT_MAXZOOM,
            )
          } else {
            const { lng, lat } = hit.lngLat
            fitBox(
              [
                [lng - POINT_BOUNDS_EPS, lat - POINT_BOUNDS_EPS],
                [lng + POINT_BOUNDS_EPS, lat + POINT_BOUNDS_EPS],
              ],
              12,
            )
          }
        })
      },
      getCenter: () => {
        const center = map.getCenter()
        return { lng: center.lng, lat: center.lat }
      },
      isAtHomeView: (place: ResolvedPlace) => {
        const home = homeCamera(map, place, chrome())
        const c = map.getCenter()
        // Pixel tolerance → degrees at the home zoom, so the same few-px slack
        // holds whether we're framing a country or a city block.
        const tol = (360 / (512 * Math.pow(2, home.zoom))) * HOME_PAN_PX
        return (
          Math.abs(map.getZoom() - home.zoom) < HOME_ZOOM_EPS &&
          Math.abs(c.lng - home.center.lng) < tol &&
          Math.abs(c.lat - home.center.lat) < tol
        )
      },
    }

    map.on('load', () => {
      // Every basemap layer that draws a feature's name (skips shields,
      // which label the `ref` property).
      const labelLayers: LabelLayer[] = []
      for (const layer of map.getStyle().layers ?? []) {
        // Commercial POIs are not toponyms (see poi.ts). Keyed on the
        // source layer, not the style's layer ids (poi_r1, poi_transit,
        // ...): those are OpenFreeMap's own naming and may be restyled,
        // while `poi` is the OpenMapTiles schema. Hiding them is also
        // what un-clicks them — queryRenderedFeatures only returns
        // features from layers in the style.
        if ('source-layer' in layer && layer['source-layer'] === 'poi') {
          map.setFilter(layer.id, poiClassFilter(layer.filter))
        }
        if (layer.type !== 'symbol') continue
        const textField = layer.layout?.['text-field']
        if (!textField || !JSON.stringify(textField).includes('"name"')) {
          continue
        }
        const sourceLayer =
          'source-layer' in layer && typeof layer['source-layer'] === 'string'
            ? layer['source-layer']
            : ''
        const originalColor = layer.paint?.['text-color'] ?? '#333'
        const spatial = sourceLayer === SPATIAL_SOURCE_LAYER
        labelLayers.push({
          id: layer.id,
          originalColor,
          classExpr: labelClassExpr(sourceLayer),
          spatial,
        })
        // Spatial layers are colored by feature-state (tier 2); set that
        // wrapper once here, then reconcilePlaceHighlights toggles the state.
        if (spatial) {
          map.setPaintProperty(layer.id, 'text-color', spatialColor(originalColor))
        }
        // Chosen-language labels (replace the style's latin\nnonlatin stack)
        map.setLayoutProperty(
          layer.id,
          'text-field',
          nameField(propsRef.current.labelLanguage),
        )
      }
      labelLayersRef.current = labelLayers

      // Its own source, not `highlights`: that one is replaced wholesale
      // on every moveend from the viewport query, which would clobber it.
      map.addSource('focus-geometry', {
        type: 'geojson',
        data: EMPTY_COLLECTION,
      })
      map.addLayer(
        {
          id: 'focus-line',
          type: 'line',
          source: 'focus-geometry',
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: {
            'line-color': DOT_COLOR,
            'line-opacity': 0.85,
            // Thin enough at the framing zoom to show a river's shape,
            // heavier once you're in close.
            'line-width': ['interpolate', ['linear'], ['zoom'], 4, 2.5, 12, 5],
          },
        },
        // Under the basemap's names, so the labels stay readable through it.
        labelLayers[0]?.id,
      )

      map.addSource('highlights', {
        type: 'geojson',
        data: EMPTY_COLLECTION,
      })
      map.addLayer({
        id: 'article-dots',
        type: 'circle',
        source: 'highlights',
        layout: {
          visibility: propsRef.current.allArticles ? 'visible' : 'none',
        },
        paint: {
          'circle-color': DOT_COLOR,
          'circle-radius': 5,
          'circle-opacity': 0.9,
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 1.5,
        },
      })
      readyRef.current = true
      refreshHighlights(map)
      propsRef.current.onViewportChange()
      pendingFly?.()
      pendingFly = null
    })
    map.on('idle', () => {
      if (!readyRef.current) return
      reconcilePlaceHighlights(map)
      updateDotFilter(map)
    })
    map.on('moveend', () => {
      // Our fly has landed; from here any move is the user's, and the
      // next one takes the highlight down.
      focusFlyingRef.current = false
      if (readyRef.current) {
        refreshHighlights(map)
        propsRef.current.onViewportChange()
      }
    })
    map.on('mouseenter', 'article-dots', () => {
      map.getCanvas().style.cursor = 'pointer'
    })
    map.on('mouseleave', 'article-dots', () => {
      map.getCanvas().style.cursor = ''
    })

    map.on('click', (e) => {
      // Read per click rather than latched: a tablet with a keyboard folio
      // switches pointer type mid-session.
      const t = window.matchMedia(COARSE_QUERY).matches
        ? TOUCH_TOLERANCE_PX
        : CLICK_TOLERANCE_PX
      const box: [maplibregl.PointLike, maplibregl.PointLike] = [
        [e.point.x - t, e.point.y - t],
        [e.point.x + t, e.point.y + t],
      ]
      const dots = readyRef.current
        ? map.queryRenderedFeatures(box, { layers: ['article-dots'] })
        : []
      propsRef.current.onClickFeatures(
        toCandidates(
          map.queryRenderedFeatures(box),
          dots,
          propsRef.current.labelLanguage,
        ),
        { x: e.point.x, y: e.point.y },
        {
          lngLat: { lng: e.lngLat.lng, lat: e.lngLat.lat },
          zoom: map.getZoom(),
        },
      )
    })
    map.on('movestart', (e) => {
      // The highlight answers a question the user just asked, and the
      // fly that frames it is itself a movestart — so clearing naively
      // here would kill it on the frame it appeared. Two signals say the
      // move is the user's: an originalEvent (a drag, even one that
      // interrupts our fly mid-flight), or the latch already down
      // because our fly settled. Either alone is unreliable —
      // originalEvent is absent for keyboard nav and inertial drift —
      // so honour both, the same belt-and-braces as App's pendingHomeRef.
      if (e.originalEvent || !focusFlyingRef.current) clearFocus()
      propsRef.current.onMoveStart()
    })

    return () => {
      abortRef.current?.abort()
      focusAbortRef.current?.abort()
      readyRef.current = false
      mapRef.current = null
      mapApi.current = null
      map.remove()
    }
    // mapApi is a stable ref object from App
  }, [mapApi])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !readyRef.current) return
    map.setLayoutProperty(
      'article-dots',
      'visibility',
      allArticles ? 'visible' : 'none',
    )
  }, [allArticles])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !readyRef.current) return
    refreshHighlights(map)
  }, [highlightsEpoch])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !readyRef.current) return
    for (const { id } of labelLayersRef.current) {
      map.setLayoutProperty(id, 'text-field', nameField(labelLanguage))
    }
    applyLabelColors(map)
    // relayout re-renders; 'idle' then refreshes the dot filter
  }, [labelLanguage])

  return <div ref={containerRef} className="map-container" />
}

export default MapView
