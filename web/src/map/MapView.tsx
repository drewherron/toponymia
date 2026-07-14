import type { FeatureCollection } from 'geojson'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useEffect, useRef } from 'react'
import { fetchHighlights } from '../api'
import type { ClickContext, FeatureCandidate } from '../types'
import { toCandidates } from './features'

const MAP_STYLE = 'https://tiles.openfreemap.org/styles/liberty'
const CLICK_TOLERANCE_PX = 6
const HIGHLIGHT_COLOR = '#d97706'

const EMPTY_COLLECTION: FeatureCollection = {
  type: 'FeatureCollection',
  features: [],
}

interface MapViewProps {
  onClickFeatures: (
    candidates: FeatureCandidate[],
    point: { x: number; y: number },
    click: ClickContext,
  ) => void
  onMoveStart: () => void
  /** Dim the basemap so only highlighted (article-bearing) features pop. */
  articlesOnly: boolean
  /** Bump to force a highlight refetch (e.g. after saving an article). */
  highlightsEpoch: number
}

function MapView({
  onClickFeatures,
  onMoveStart,
  articlesOnly,
  highlightsEpoch,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
  const layersReadyRef = useRef(false)
  const abortRef = useRef<AbortController | null>(null)
  const propsRef = useRef({ onClickFeatures, onMoveStart, articlesOnly })

  useEffect(() => {
    propsRef.current = { onClickFeatures, onMoveStart, articlesOnly }
  }, [onClickFeatures, onMoveStart, articlesOnly])

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
        const source = map.getSource('highlights') as
          | maplibregl.GeoJSONSource
          | undefined
        source?.setData(collection)
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

    map.on('load', () => {
      // Semi-opaque veil over the whole basemap; the highlight layers sit
      // above it, so toggling it on leaves only article geometry vivid.
      map.addLayer({
        id: 'articles-only-dim',
        type: 'background',
        layout: {
          visibility: propsRef.current.articlesOnly ? 'visible' : 'none',
        },
        paint: {
          'background-color': '#f5f2ec',
          'background-opacity': 0.8,
        },
      })
      map.addSource('highlights', {
        type: 'geojson',
        data: EMPTY_COLLECTION,
      })
      map.addLayer({
        id: 'highlight-line-glow',
        type: 'line',
        source: 'highlights',
        filter: ['==', ['geometry-type'], 'LineString'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': HIGHLIGHT_COLOR,
          'line-opacity': 0.3,
          'line-blur': 4,
          'line-width': [
            'interpolate', ['linear'], ['zoom'], 4, 6, 10, 12, 16, 20,
          ],
        },
      })
      map.addLayer({
        id: 'highlight-line',
        type: 'line',
        source: 'highlights',
        filter: ['==', ['geometry-type'], 'LineString'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': HIGHLIGHT_COLOR,
          'line-opacity': 0.85,
          'line-width': [
            'interpolate', ['linear'], ['zoom'], 4, 1.5, 10, 3, 16, 5,
          ],
        },
      })
      map.addLayer({
        id: 'highlight-point-glow',
        type: 'circle',
        source: 'highlights',
        filter: ['==', ['geometry-type'], 'Point'],
        paint: {
          'circle-color': HIGHLIGHT_COLOR,
          'circle-radius': 14,
          'circle-opacity': 0.25,
          'circle-blur': 0.8,
        },
      })
      map.addLayer({
        id: 'highlight-point',
        type: 'circle',
        source: 'highlights',
        filter: ['==', ['geometry-type'], 'Point'],
        paint: {
          'circle-color': HIGHLIGHT_COLOR,
          'circle-radius': 4.5,
          'circle-opacity': 0.9,
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 1.5,
        },
      })
      layersReadyRef.current = true
      refreshHighlights(map)
    })
    map.on('moveend', () => refreshHighlights(map))

    map.on('click', (e) => {
      const t = CLICK_TOLERANCE_PX
      const features = map.queryRenderedFeatures([
        [e.point.x - t, e.point.y - t],
        [e.point.x + t, e.point.y + t],
      ])
      propsRef.current.onClickFeatures(
        toCandidates(features),
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
      layersReadyRef.current = false
      mapRef.current = null
      map.remove()
    }
  }, [])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !layersReadyRef.current) return
    map.setLayoutProperty(
      'articles-only-dim',
      'visibility',
      articlesOnly ? 'visible' : 'none',
    )
  }, [articlesOnly])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !layersReadyRef.current) return
    refreshHighlights(map)
  }, [highlightsEpoch])

  return <div ref={containerRef} className="map-container" />
}

export default MapView
