import type { FeatureCollection } from 'geojson'
import maplibregl from 'maplibre-gl'
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

const MAP_STYLE = 'https://tiles.openfreemap.org/styles/liberty'
const CLICK_TOLERANCE_PX = 6
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
  /** Show dots for articles whose basemap label isn't rendered here. */
  allArticles: boolean
  /** Bump to force a highlight refetch (e.g. after saving an article). */
  highlightsEpoch: number
  /** Receives imperative controls (fly-to for search/deep links). */
  mapApi: RefObject<MapApi | null>
}

function MapView({
  onClickFeatures,
  onMoveStart,
  allArticles,
  highlightsEpoch,
  mapApi,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
  const readyRef = useRef(false)
  const abortRef = useRef<AbortController | null>(null)
  const propsRef = useRef({ onClickFeatures, onMoveStart, allArticles })
  const labelLayersRef = useRef<LabelLayer[]>([])
  const articleNamesRef = useRef<string[]>([])
  const collectionRef = useRef<FeatureCollection>(EMPTY_COLLECTION)
  const hiddenDotsRef = useRef('')

  useEffect(() => {
    propsRef.current = { onClickFeatures, onMoveStart, allArticles }
  }, [onClickFeatures, onMoveStart, allArticles])

  /** Wrap every label layer's text color: article names go amber. */
  const applyLabelColors = (map: maplibregl.Map) => {
    const names = articleNamesRef.current
    for (const { id, originalColor } of labelLayersRef.current) {
      map.setPaintProperty(id, 'text-color', [
        'case',
        ['in', ['get', 'name'], ['literal', names]],
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
    for (const feature of map.queryRenderedFeatures({ layers: layerIds })) {
      const name = feature.properties?.name
      if (typeof name === 'string') renderedNames.add(name)
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

    // A camera animation started before the style loads gets dropped,
    // so a fly requested at boot (deep link) waits for 'load'.
    let pendingFly: (() => void) | null = null
    const flyWhenReady = (fly: () => void) => {
      if (map.loaded()) fly()
      else pendingFly = fly
    }
    const fitBbox = ([w, s, e, n]: [number, number, number, number]) => {
      map.fitBounds(
        [
          [w, s],
          [e, n],
        ],
        { padding: 80, maxZoom: 14 },
      )
    }
    mapApi.current = {
      flyToPlace: (place: ResolvedPlace) => {
        flyWhenReady(() => {
          if (place.bbox) {
            fitBbox(place.bbox)
          } else {
            const [lng, lat] = place.label_point ?? place.centroid
            map.flyTo({ center: [lng, lat], zoom: 12 })
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
      pendingFly?.()
      pendingFly = null
    })
    map.on('idle', () => {
      if (readyRef.current) updateDotFilter(map)
    })
    map.on('moveend', () => {
      if (readyRef.current) refreshHighlights(map)
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
        toCandidates(map.queryRenderedFeatures(box), dots),
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

  return <div ref={containerRef} className="map-container" />
}

export default MapView
