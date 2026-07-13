export interface FeatureCandidate {
  name: string
  kind: string
  sourceLayer: string
  properties: Record<string, unknown>
}

/** Where on the map the candidates were clicked. */
export interface ClickContext {
  lngLat: { lng: number; lat: number }
  zoom: number
}

export interface ResolvedPlace {
  id: number
  slug: string
  display_name: string
  feature_class: string
  anchor_level: 'wikidata' | 'osm' | 'name'
  wikidata_qid: string | null
  osm_type: string | null
  osm_id: number | null
  centroid: [number, number]
}

export interface ResolveResponse {
  place: ResolvedPlace
  created: boolean
}
