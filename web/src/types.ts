export interface FeatureCandidate {
  name: string
  kind: string
  sourceLayer: string
  properties: Record<string, unknown>
  /** Set when the candidate is an article dot: selection can skip
   *  resolution and fetch the place directly. */
  slug?: string
  /** The feature's own point (label anchors are exact), preferred over
   *  the mouse position when resolving — a zoomed-out click can land
   *  kilometres from the feature it visually hits. */
  anchor?: { lng: number; lat: number }
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

export interface RevisionSummary {
  id: number
  author: string
  created: string
  comment: string
  is_current: boolean
}

export interface RevisionDetail extends RevisionSummary {
  content: ArticleContent
}

export interface TalkPost {
  id: number
  author: string
  body_md: string
  created: string
  edited: string | null
}

export interface TalkThread {
  id: number
  title: string
  created: string
  posts: TalkPost[]
}
