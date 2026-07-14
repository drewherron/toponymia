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

export interface NameEntry {
  name: string
  language: string
  from_languages: string[]
  is_endonym: boolean
  etymology_md: string
  references: string[]
}

export interface Derivation {
  term: string
  note: string
  url: string
}

export interface ArticleContent {
  body_md: string
  names: NameEntry[]
  derivations: Derivation[]
  see_also: string[]
}

export interface ArticleData {
  content: ArticleContent
  revision_id: number
  author: string
  created: string
  comment: string
  protection_level: string
}

export interface PlaceDetail {
  place: ResolvedPlace
  article: ArticleData | null
}

export interface User {
  id: number
  username: string
}
