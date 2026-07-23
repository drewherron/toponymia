import type { FeatureCollection } from 'geojson'
// Relative path: the package's `exports` map hides its dist build, but
// MapLibre's plugin loader needs the dist UMD file, served as an asset.
import rtlTextUrl from '../../node_modules/@mapbox/mapbox-gl-rtl-text/dist/mapbox-gl-rtl-text.js?url'
import maplibregl from 'maplibre-gl'
import type { ExpressionSpecification } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useEffect, useRef } from 'react'
import type { RefObject } from 'react'
import { fetchHighlights } from '../api'
import type {
  ClickContext,
  FeatureCandidate,
  GeocodeHit,
  MapApi,
  ResolvedPlace,
} from '../types'
import { toCandidates } from './features'
import { nameField, nameKeys, nameMatch } from './labels'

const MAP_STYLE = 'https://tiles.openfreemap.org/styles/liberty'
const CLICK_TOLERANCE_PX = 6
// Framing a place gets flown to — shared by flyToPlace and the "home view"
// the recenter button compares against, so the two never disagree.
const FIT_PADDING = 80
const FIT_MAXZOOM = 14
const FLY_ZOOM = 12
// How far the live camera may drift from the home view and still count as
// "there" — a few screen px of pan and a hair of zoom, so the fly landing's
// rounding isn't read as movement while any real pan/zoom is.
const HOME_PAN_PX = 6
const HOME_ZOOM_EPS = 0.05

/** The camera flyToPlace lands on: fit the bbox when we have one, else
 *  center on the label point at a default zoom. cameraForBounds computes the
 *  fit without moving the map, so it matches fitBounds exactly. */
function homeCamera(
  map: maplibregl.Map,
  place: ResolvedPlace,
): { center: { lng: number; lat: number }; zoom: number } {
  if (place.bbox) {
    const [w, s, e, n] = place.bbox
    const cam = map.cameraForBounds(
      [
        [w, s],
        [e, n],
      ],
      { padding: FIT_PADDING, maxZoom: FIT_MAXZOOM },
    )
    if (cam?.center && cam.zoom != null) {
      const center = maplibregl.LngLat.convert(cam.center)
      return { center: { lng: center.lng, lat: center.lat }, zoom: cam.zoom }
    }
  }
  const [lng, lat] = place.label_point ?? place.centroid
  return { center: { lng, lat }, zoom: FLY_ZOOM }
}

// Shapes Arabic/Hebrew label text (self-hosted; lazy = fetched only when
// RTL text first appears in view). Without it RTL names render backward.
maplibregl.setRTLTextPlugin(rtlTextUrl, true).catch(console.error)

const RAW_NAME_MATCH: ExpressionSpecification = [
  'coalesce',
  ['get', 'name'],
  '',
]
// Darker amber for label text (readability at small sizes), brighter for dots.
const LABEL_COLOR = '#b45309'
const DOT_COLOR = '#d97706'

const EMPTY_COLLECTION: FeatureCollection = {
  type: 'FeatureCollection',
  features: [],
}

interface LabelLayer {
  id: string
  originalColor: unknown
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
  })
  const labelLayersRef = useRef<LabelLayer[]>([])
  const articleNamesRef = useRef<string[]>([])
  const collectionRef = useRef<FeatureCollection>(EMPTY_COLLECTION)
  const hiddenDotsRef = useRef('')

  useEffect(() => {
    propsRef.current = {
      onClickFeatures,
      onMoveStart,
      onViewportChange,
      allArticles,
      labelLanguage,
    }
  }, [onClickFeatures, onMoveStart, onViewportChange, allArticles, labelLanguage])

  /** Wrap every label layer's text color: article names go amber.
   *  Matches the displayed name, the raw `name`, and the English name
   *  (article names are English by construction), so places light up
   *  whatever the label language. */
  const applyLabelColors = (map: maplibregl.Map) => {
    const names = articleNamesRef.current
    const lang = propsRef.current.labelLanguage
    const matches: ExpressionSpecification[] = [
      ['in', nameMatch(lang), ['literal', names]],
      ['in', RAW_NAME_MATCH, ['literal', names]],
    ]
    if (lang !== 'en') {
      matches.push(['in', nameMatch('en'), ['literal', names]])
    }
    for (const { id, originalColor } of labelLayersRef.current) {
      map.setPaintProperty(id, 'text-color', [
        'case',
        ['any', ...matches],
        LABEL_COLOR,
        originalColor,
      ])
    }
  }

  /**
   * A dot only stands in for a missing label: hide it as soon as any of
   * the place's names is currently rendered as a basemap label. Runs on
   * 'idle', so the dot→label handoff follows tile/collision changes.
   */
  const updateDotFilter = (map: maplibregl.Map) => {
    const layerIds = labelLayersRef.current.map((layer) => layer.id)
    const renderedNames = new Set<string>()
    const keys = [
      ...new Set([...nameKeys(propsRef.current.labelLanguage), ...nameKeys('en')]),
    ]
    for (const feature of map.queryRenderedFeatures({ layers: layerIds })) {
      const props = feature.properties ?? {}
      for (const key of keys) {
        const value = props[key]
        if (typeof value === 'string' && value) renderedNames.add(value)
      }
    }
    const hidden: string[] = []
    for (const feature of collectionRef.current.features) {
      const props = feature.properties as {
        slug?: string
        names?: string[]
      } | null
      if (!props?.slug) continue
      if ((props.names ?? []).some((name) => renderedNames.has(name))) {
        hidden.push(props.slug)
      }
    }
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
        const names = new Set<string>()
        for (const feature of collection.features) {
          const list = (feature.properties as { names?: string[] })?.names
          for (const name of list ?? []) names.add(name)
        }
        articleNamesRef.current = [...names]
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
    const fitBbox = (
      [w, s, e, n]: [number, number, number, number],
      animate = true,
    ) => {
      map.fitBounds(
        [
          [w, s],
          [e, n],
        ],
        { padding: FIT_PADDING, maxZoom: FIT_MAXZOOM, animate },
      )
    }
    mapApi.current = {
      flyToPlace: (place: ResolvedPlace, animate = true) => {
        flyWhenReady(() => {
          if (place.bbox) {
            fitBbox(place.bbox, animate)
          } else {
            const [lng, lat] = place.label_point ?? place.centroid
            map.flyTo({ center: [lng, lat], zoom: FLY_ZOOM, animate })
          }
        })
      },
      flyToHit: (hit: GeocodeHit) => {
        flyWhenReady(() => {
          if (hit.extent) fitBbox(hit.extent)
          else map.flyTo({ center: hit.lngLat, zoom: 12 })
        })
      },
      getCenter: () => {
        const center = map.getCenter()
        return { lng: center.lng, lat: center.lat }
      },
      isAtHomeView: (place: ResolvedPlace) => {
        const home = homeCamera(map, place)
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
        if (layer.type !== 'symbol') continue
        const textField = layer.layout?.['text-field']
        if (!textField || !JSON.stringify(textField).includes('"name"')) {
          continue
        }
        labelLayers.push({
          id: layer.id,
          originalColor: layer.paint?.['text-color'] ?? '#333',
        })
        // Chosen-language labels (replace the style's latin\nnonlatin stack)
        map.setLayoutProperty(
          layer.id,
          'text-field',
          nameField(propsRef.current.labelLanguage),
        )
      }
      labelLayersRef.current = labelLayers

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
      if (readyRef.current) updateDotFilter(map)
    })
    map.on('moveend', () => {
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
      const t = CLICK_TOLERANCE_PX
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
    map.on('movestart', () => {
      propsRef.current.onMoveStart()
    })

    return () => {
      abortRef.current?.abort()
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
