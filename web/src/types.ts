import type { Feature } from 'geojson'

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

/** Every place the signed-in user has edited or discussed, fetched whole
 *  rather than per viewport — see the server's `contributions` view. */
export interface Contributions {
  type: 'FeatureCollection'
  features: Feature[]
  /** Framing box over the dots; null when there are none to frame.
   *  Its own field rather than GeoJSON's optional `bbox`, which is typed
   *  as "2D or 3D box, or absent" — this one is always the flat four. */
  bbox: [number, number, number, number] | null
  /** The user is past the server's cap, so this is a partial footprint. */
  truncated: boolean
}

/** The chrome overlaying the map, in CSS px per edge — the camera keeps a
 *  place inside what's left. */
export interface MapPadding {
  top: number
  right: number
  bottom: number
  left: number
}

/** Imperative map controls exposed by MapView through a ref object. */
export interface MapApi {
  /** animate: false jumps straight to the framing (boot deep links). */
  flyToPlace: (place: ResolvedPlace, animate?: boolean) => void
  flyToHit: (hit: GeocodeHit) => void
  /** Frame an arbitrary box — the contributions lens landing on the
   *  user's whole footprint. */
  flyToBounds: (bbox: [number, number, number, number]) => void
  getCenter: () => { lng: number; lat: number }
  /** Is the live camera still at the place's canonical framing (the view
   *  flyToPlace lands on)? Drives the pane's "recenter" affordance — it
   *  appears the moment you leave that view, even if the place is still
   *  visible. */
  isAtHomeView: (place: ResolvedPlace) => boolean
  /** Draw the place's own course over the basemap, answering "where does
   *  this thing run?" at the framing zoom a dot can't. Transient by
   *  design: the map clears it on the first user-initiated move. */
  showFocusGeometry: (slug: string) => void
  clearFocusGeometry: () => void
}

export interface ResolveResponse {
  place: ResolvedPlace
  created: boolean
}

/** How sure the field is that an etymology is right. '' means nobody
 *  said, which is not the same claim as 'unknown'. */
export type Confidence =
  | ''
  | 'attested'
  | 'probable'
  | 'proposed'
  | 'disputed'
  | 'folk'
  | 'unknown'

export type ElementRole = '' | 'generic' | 'specific' | 'affix' | 'connective'

/** One etymon — a word the name is built from. */
export interface Element {
  form: string
  language: string
  gloss: string
  role: ElementRole
  script: string
  transliteration: string
}

/** One hypothesis about where a name comes from. Source languages and
 *  references hang off the hypothesis, not the name: rival theories cite
 *  different languages and different sources. */
export interface Etymology {
  etymology_md: string
  confidence: Confidence
  from_languages: string[]
  elements: Element[]
  references: string[]
}

export interface NameEntry {
  name: string
  language: string
  is_endonym: boolean
  /** Editorial order — the first entry is the primary one. */
  etymologies: Etymology[]
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

/** Who deleted a soft-deleted article and when. Admin-only: null for
 *  everyone else, who can't tell a deleted article from an unwritten one. */
export interface ArticleDeletion {
  at: string
  by: string | null
}

export interface PlaceDetail {
  place: ResolvedPlace
  article: ArticleData | null
  /** Live threads on this place, for the Talk tab's count. Uncapped,
   *  unlike the talk listing itself. */
  talk_thread_count: number
  /** Present even for a locked stub with no article yet. */
  protection_level: ProtectionLevel
  /** Non-null only for an admin looking at a deleted article. */
  deleted: ArticleDeletion | null
}

export interface User {
  id: number
  username: string
  email: string
  is_moderator: boolean
  is_admin: boolean
}

/** `/api/me/`: the session probe, plus whether registration is open at all
 *  (closed during the pre-launch window — see `PRELAUNCH` in settings.py). */
export interface Me {
  user: User | null
  signupsOpen: boolean
}

export interface RevisionSummary {
  id: number
  author: string
  created: string
  comment: string
  is_current: boolean
  /** Removed from public view. The row still appears in history — it is the
   *  attribution record for text that may still be live — but the author and
   *  timestamp are all a non-moderator gets: the comment comes back empty and
   *  the snapshot 404s. */
  suppressed: boolean
  /** Whether *you* have reported this, ever — not whether anyone has. Stays
   *  true after a moderator closes the report, because it is what stops the
   *  same person filing the same complaint twice. Always false when logged
   *  out. */
  reported: boolean
}

export interface RevisionDetail extends RevisionSummary {
  content: ArticleContent
}

/** One page of edit history. The server caps how many revisions a single
 *  response carries, so a long-running edit war stays reachable without ever
 *  being assembled in one response. */
export interface RevisionPage {
  revisions: RevisionSummary[]
  /** Total revisions visible to this viewer, across all pages. */
  total: number
  offset: number
  has_more: boolean
}

export interface TalkPost {
  id: number
  /** Reads `[deleted]` on a removed post unless the viewer is a moderator —
   *  naming the author of hidden abuse would republish the association the
   *  removal was meant to end. */
  author: string
  body_md: string
  created: string
  edited: string | null
  /** A removed post stays as a tombstone; body_md comes back empty. */
  deleted: boolean
  /** Whether *you* have reported this — see `RevisionSummary.reported`. */
  reported: boolean
}

export interface TalkThread {
  id: number
  title: string
  created: string
  posts: TalkPost[]
  /** The server caps posts per thread; true means later replies aren't here. */
  posts_truncated: boolean
}

/** The threads shown for one place, and whether the cap hid any. */
export interface TalkPage {
  threads: TalkThread[]
  has_more: boolean
}

/** One row in the moderator queue: a report with its target's context. */
export type ReportCategory =
  | 'spam'
  | 'vandalism'
  | 'harassment'
  | 'personal_info'
  | 'copyright'
  | 'other'

/** User-facing labels for report categories. */
export const REPORT_CATEGORIES: { value: ReportCategory; label: string }[] = [
  { value: 'spam', label: 'Spam' },
  { value: 'vandalism', label: 'Vandalism' },
  { value: 'harassment', label: 'Harassment' },
  { value: 'personal_info', label: 'Personal information' },
  { value: 'copyright', label: 'Copyright' },
  { value: 'other', label: 'Other' },
]

export interface ReportRow {
  id: number
  category: ReportCategory
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
  suppressed: boolean
}

export interface TalkPostReportTarget extends ReportTargetBase {
  kind: 'talk_post'
  thread_id: number
  thread_title: string
  deleted: boolean
}

export type ReportTarget = RevisionReportTarget | TalkPostReportTarget

export type ReportAction = 'resolve' | 'dismiss' | 'delete' | 'suppress'

// --- Moderation dashboard ---------------------------

export type UserRole = 'user' | 'moderator' | 'admin'

export interface ModUserRow {
  id: number
  username: string
  role: UserRole
  reports_open: number
  reports_total: number
  removed_count: number
  upheld_actions: number
  last_report: string | null
  banned: boolean
}

export interface ModUsersResult {
  users: ModUserRow[]
  /** True when the roster hit the server's cap — the client filter can only
   *  search what it was sent, so the UI has to admit the list is partial. */
  truncated: boolean
}

export interface ModBan {
  id: number
  reason: string
  created: string
  created_by: string | null
  expires: string | null
  lifted: string | null
  lifted_by: string | null
  active: boolean
}

export interface ModUserPost {
  id: number
  thread_id: number
  thread_title: string
  slug: string
  place: string
  body_md: string
  created: string
  deleted: boolean
}

/** A thread this user opened. Removal here is coarser than a post's: it
 *  hides everyone's replies too, hence its own list and its own restore. */
export interface ModUserThread {
  id: number
  title: string
  slug: string
  place: string
  post_count: number
  created: string
  deleted: boolean
}

export interface ModUserRevision {
  id: number
  slug: string
  place: string
  comment: string
  excerpt: string
  created: string
  is_current: boolean
  suppressed: boolean
}

export interface ModUserReport {
  id: number
  category: ReportCategory
  reason: string
  status: 'open' | 'resolved' | 'dismissed'
  reporter: string
  created: string
  target_kind: 'revision' | 'talk_post'
}

export interface ModAuditEntry {
  id: number
  action: string
  actor: string | null
  reason: string
  created: string
}

export interface ModUserDetail {
  id: number
  username: string
  role: UserRole
  date_joined: string
  bans: ModBan[]
  can_ban: boolean
  can_set_role: boolean
  talk_posts: ModUserPost[]
  talk_threads: ModUserThread[]
  revisions: ModUserRevision[]
  reports_against: ModUserReport[]
  audit: ModAuditEntry[]
}

export interface ModReporter {
  id: number
  username: string
  total: number
  open: number
  resolved: number
  dismissed: number
}

/** One row of the global audit feed — the oversight lens that answers
 *  "is a moderator quietly working through every article on the wiki?",
 *  which the per-user trail structurally cannot. */
export interface ModAuditRow {
  id: number
  action: string
  actor: string | null
  target_user: string | null
  reason: string
  created: string
  place_slug: string | null
}

/** One page of the global audit feed. The log only grows, so this is a
 *  browsable archive rather than a capped window — `total` is what lets the
 *  client number the pages. */
export interface ModAuditPage {
  actions: ModAuditRow[]
  total: number
  offset: number
  page_size: number
}

/** What the audit feed is narrowed to. Both optional — the unfiltered feed is
 *  the default view. `actions` is a group of kinds, not one kind, because the
 *  questions worth asking ("what has been removed") span several. */
export interface ModAuditFilters {
  target?: number
  actions?: string[]
}

export interface BanInput {
  reason: string
  expires_days: number
  remove_content: boolean
}

/** What a ban's content removal took out of public view. Null rather than
 *  all-zeroes when removal wasn't requested, so "removed nothing" and "wasn't
 *  asked to remove anything" stay distinguishable. */
export interface RemovedContent {
  talk_posts: number
  revisions: number
  articles_reverted: number
  articles_deleted: number
}
