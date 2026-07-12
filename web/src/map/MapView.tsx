import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useEffect, useRef } from 'react'

const MAP_STYLE = 'https://tiles.openfreemap.org/styles/liberty'

function MapView() {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!containerRef.current) return

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: MAP_STYLE,
      center: [10, 35],
      zoom: 2,
    })
    map.addControl(new maplibregl.NavigationControl(), 'top-right')

    return () => map.remove()
  }, [])

  return <div ref={containerRef} className="map-container" />
}

export default MapView
