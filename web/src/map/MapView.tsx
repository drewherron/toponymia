import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useEffect, useRef } from 'react'
import type { ClickContext, FeatureCandidate } from '../types'
import { toCandidates } from './features'

const MAP_STYLE = 'https://tiles.openfreemap.org/styles/liberty'
const CLICK_TOLERANCE_PX = 6

interface MapViewProps {
  onClickFeatures: (
    candidates: FeatureCandidate[],
    point: { x: number; y: number },
    click: ClickContext,
  ) => void
  onMoveStart: () => void
}

function MapView({ onClickFeatures, onMoveStart }: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const handlersRef = useRef({ onClickFeatures, onMoveStart })

  useEffect(() => {
    handlersRef.current = { onClickFeatures, onMoveStart }
  }, [onClickFeatures, onMoveStart])

  useEffect(() => {
    if (!containerRef.current) return

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: MAP_STYLE,
      center: [10, 35],
      zoom: 2,
      hash: true,
    })
    map.addControl(new maplibregl.NavigationControl(), 'top-right')

    map.on('click', (e) => {
      const t = CLICK_TOLERANCE_PX
      const features = map.queryRenderedFeatures([
        [e.point.x - t, e.point.y - t],
        [e.point.x + t, e.point.y + t],
      ])
      handlersRef.current.onClickFeatures(
        toCandidates(features),
        { x: e.point.x, y: e.point.y },
        {
          lngLat: { lng: e.lngLat.lng, lat: e.lngLat.lat },
          zoom: map.getZoom(),
        },
      )
    })
    map.on('movestart', () => {
      handlersRef.current.onMoveStart()
    })

    return () => map.remove()
  }, [])

  return <div ref={containerRef} className="map-container" />
}

export default MapView
