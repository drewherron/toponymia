export interface FeatureCandidate {
  /** English-first display name — what the map label shows. */
  name: string
  /** The tile's own `name` (native), what OSM/Overpass match on.
   *  Absent for candidates that never resolve (dots, slugs). */
  rawName?: string
  /** The English name when it differs from rawName — sent to resolve as
   *  name_en regardless of the displayed label language. */
  nameEn?: string
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
  /** A point on the feature itself — where fly-to should land. */
  label_point: [number, number] | null
  /** [minLng, minLat, maxLng, maxLat] when a footprint is cached. */
  bbox: [number, number, number, number] | null
}

export interface SearchResult extends ResolvedPlace {
  /** The alias that matched when the display name itself didn't. */
  matched_name: string | null
}

/** One geocoder suggestion (Photon), for places we have no article on. */
export interface GeocodeHit {
  name: string
  /** Mapped onto the same kinds map clicks produce, for resolve caching. */
  kind: string
  /** Disambiguation line: city, state, country. */
  context: string
  lngLat: { lng: number; lat: number }
  extent: [number, number, number, number] | null
  /** "node/123" — used to drop hits that duplicate an article result. */
  osmRef: string | null
}

/** Imperative map controls exposed by MapView through a ref object. */
export interface MapApi {
  flyToPlace: (place: ResolvedPlace) => void
  flyToHit: (hit: GeocodeHit) => void
  getCenter: () => { lng: number; lat: number }
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

export type ProtectionLevel = 'none' | 'registered' | 'admin'

export interface PlaceDetail {
  place: ResolvedPlace
  article: ArticleData | null
  /** Present even for a locked stub with no article yet. */
  protection_level: ProtectionLevel
}

export interface User {
  id: number
  username: string
  is_moderator: boolean
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
  /** A removed post stays as a tombstone; body_md comes back empty. */
  deleted: boolean
}

export interface TalkThread {
  id: number
  title: string
  created: string
  posts: TalkPost[]
}

/** One row in the moderator queue: a report with its target's context. */
export interface ReportRow {
  id: number
  reason: string
  reporter: string
  created: string
  target: ReportTarget | null
}

interface ReportTargetBase {
  id: number
  author: string
  excerpt: string
  slug: string
  place: string
}

export interface RevisionReportTarget extends ReportTargetBase {
  kind: 'revision'
  comment: string
  is_current: boolean
}

export interface TalkPostReportTarget extends ReportTargetBase {
  kind: 'talk_post'
  thread_id: number
  thread_title: string
  deleted: boolean
}

export type ReportTarget = RevisionReportTarget | TalkPostReportTarget

export type ReportAction = 'resolve' | 'dismiss' | 'delete'
