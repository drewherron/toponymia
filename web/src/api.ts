import type { FeatureCollection, Geometry } from 'geojson'
import type {
  ArticleContent,
  ArticleData,
  ClickContext,
  Contributions,
  GeocodeHit,
  Me,
  PlaceDetail,
  BanInput,
  ModAuditFilters,
  ModAuditPage,
  ModReporter,
  ModUserDetail,
  ModUsersResult,
  ProtectionLevel,
  ReportAction,
  ReportCategory,
  ReportRow,
  RemovedContent,
  ResolvedPlace,
  ResolveResponse,
  RevisionDetail,
  RevisionPage,
  SearchResult,
  TalkPage,
  TalkPost,
  TalkThread,
} from './types'
import { isToponymicPhotonHit } from './poi'
// The same collapse map clicks apply, so click and search agree on a park.
import { parkKind as parkKindFromOsm } from './map/features'

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/)
  return match ? decodeURIComponent(match[1]) : ''
}

function jsonHeaders(): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    'X-CSRFToken': csrfToken(),
  }
}

/** Why a resolution failed, so the pane can say whose problem it is:
 *  `unavailable` = Overpass (upstream) is busy or down, `throttled` = our
 *  own rate limit, `signin_required` = nobody has opened this place before
 *  and only accounts can create one, `failed` = anything else. */
export type ResolveFailure =
  | 'unavailable'
  | 'throttled'
  | 'signin_required'
  | 'failed'

export class ResolveError extends Error {
  readonly reason: ResolveFailure

  constructor(status: number) {
    super(`resolve failed: ${status}`)
    this.name = 'ResolveError'
    this.reason =
      status === 503
        ? 'unavailable'
        : status === 429
          ? 'throttled'
          : status === 401
            ? 'signin_required'
            : 'failed'
  }
}

/** `osmRef` ('node/1', 'way/2', 'relation/3') identifies the element when
 *  `name` cannot: geocoder hits carry localized names that Overpass's
 *  `name` match would miss. The server reads the element's own name from
 *  it and then resolves by name as usual, so a geocoder pick lands on the
 *  same place a click on the feature would. */
export async function resolveFeature(
  name: string,
  kind: string,
  click: ClickContext,
  nameEn: string | null,
  signal?: AbortSignal,
  osmRef?: string | null,
): Promise<ResolveResponse> {
  const response = await fetch('/api/resolve/', {
    method: 'POST',
    headers: jsonHeaders(),
    signal,
    body: JSON.stringify({
      name,
      class: kind,
      lngLat: [click.lngLat.lng, click.lngLat.lat],
      zoom: click.zoom,
      ...(nameEn ? { name_en: nameEn } : {}),
      ...(osmRef ? { osm_ref: osmRef } : {}),
    }),
  })
  if (!response.ok) {
    throw new ResolveError(response.status)
  }
  return response.json()
}

/** GeoJSON of places with articles in the given viewport. */
export async function fetchHighlights(
  bbox: [number, number, number, number],
  signal?: AbortSignal,
): Promise<FeatureCollection> {
  const response = await fetch(`/api/highlights/?bbox=${bbox.join(',')}`, {
    signal,
  })
  if (!response.ok) {
    throw new Error(`highlights failed: ${response.status}`)
  }
  return response.json()
}

/** Every place the signed-in user has edited or posted talk on, whole —
 *  not viewport-scoped the way highlights is, because the point of the
 *  lens is to show where you've been before you know where to look. */
export async function fetchContributions(
  signal?: AbortSignal,
): Promise<Contributions> {
  const response = await fetch('/api/me/contributions/', { signal })
  if (!response.ok) {
    throw new Error(`contributions failed: ${response.status}`)
  }
  return response.json()
}

/** A place's cached course, for the transient "zoom to place" highlight.
 *  Null for area relations, which cache no geometry. Its own request
 *  rather than a field on getPlace: it can run to tens of kB, and only
 *  the recenter button ever needs it. */
export async function fetchPlaceGeometry(
  slug: string,
  signal?: AbortSignal,
): Promise<Geometry | null> {
  const response = await fetch(`/api/places/${slug}/geometry/`, { signal })
  if (!response.ok) {
    throw new Error(`geometry fetch failed: ${response.status}`)
  }
  const body = await response.json()
  return body.geometry
}

/** Our own articles matching the query, by any of their names. */
export async function searchArticles(
  query: string,
  signal?: AbortSignal,
): Promise<SearchResult[]> {
  const response = await fetch(
    `/api/search/?q=${encodeURIComponent(query)}`,
    { signal },
  )
  if (!response.ok) {
    throw new Error(`search failed: ${response.status}`)
  }
  const body = await response.json()
  return body.results
}

/** A random place with an article, or null when there's none to give.
 * `exclude` — the slug already open — is left out of the draw, so the button
 * doesn't hand back the article the reader is looking at. */
export async function fetchRandomArticle(
  exclude?: string | null,
): Promise<ResolvedPlace | null> {
  const query = exclude ? `?not=${encodeURIComponent(exclude)}` : ''
  const response = await fetch(`/api/random/${query}`)
  if (!response.ok) {
    throw new Error(`random failed: ${response.status}`)
  }
  const body = await response.json()
  return body.place
}

/** The OSM `boundary=*` values that are parks rather than administrative
 *  divisions. `aboriginal_lands` is here because OpenMapTiles routes it to
 *  the park layer too, so a click already treats it as one. */
const PARK_BOUNDARY_VALUES = new Set([
  'national_park',
  'protected_area',
  'aboriginal_lands',
])

const PHOTON_TYPE: Record<string, string> = {
  N: 'node',
  W: 'way',
  R: 'relation',
}

/** Map Photon's osm_key/osm_value onto the kinds map clicks produce
 * (features.ts), so a geocoder pick hits the same resolve cache row a
 * click on the feature would. */
function kindFromPhoton(key: string, value: string): string {
  if (key === 'place') return value || 'place'
  if (key === 'waterway') return 'waterway'
  if (key === 'highway') return 'road'
  if (key === 'natural') return value === 'peak' ? 'peak' : value || 'water'
  // Parks before the generic `boundary`, and through the same collapse a map
  // click applies (`parkKindFromOsm`) — otherwise a search pick on Mount Hood
  // National Forest is a `boundary` while a click on it is a `protected_area`,
  // and the two mint separate Places for one feature. `boundary` stays the
  // answer for administrative boundaries, which is what it was always for.
  if (key === 'boundary' && PARK_BOUNDARY_VALUES.has(value)) {
    return parkKindFromOsm(value)
  }
  if (key === 'leisure' && value === 'nature_reserve') {
    return parkKindFromOsm(value)
  }
  if (key === 'boundary') return 'boundary'
  return value || key || 'place'
}

interface PhotonFeature {
  geometry: { coordinates: [number, number] }
  properties: {
    name?: string
    osm_key?: string
    osm_value?: string
    osm_type?: string
    osm_id?: number
    city?: string
    state?: string
    country?: string
    /** Photon order: [minLng, maxLat, maxLng, minLat]. */
    extent?: [number, number, number, number]
  }
}

/** Where a hit is, for the dropdown's second line — and half of the key
 *  rows are deduped by, so it has to be computed identically twice. */
function photonContext(props: PhotonFeature['properties']): string {
  return [props.city, props.state, props.country]
    .filter((part): part is string => !!part && part !== props.name)
    .join(', ')
}

/** Geocoder half of search (Photon — public, keyless, CORS-open).
 * Biased toward the current map view when a center is given.
 *
 * The names here are **localized, not OSM's own**: Photon honours the
 * browser's `Accept-Language` even with no `lang` parameter, so an
 * English browser gets 'Brasov' for Brașov and 'Vienna' for Wien, and a
 * browser cannot unset that header (`fetch` forbids it). That is what we
 * want on screen — a reader who searched "Beijing" should not be offered
 * 北京市 — but it is *not* what Overpass matches on, so a hit resolves by
 * `osmRef` rather than by this name. See `resolveFeature`. */
export async function searchGeocoder(
  query: string,
  center: { lng: number; lat: number } | null,
  signal?: AbortSignal,
): Promise<GeocodeHit[]> {
  const params = new URLSearchParams({ q: query, limit: '6' })
  if (center) {
    params.set('lat', center.lat.toFixed(4))
    params.set('lon', center.lng.toFixed(4))
  }
  const response = await fetch(
    `https://photon.komoot.io/api/?${params}`,
    { signal },
  )
  if (!response.ok) {
    throw new Error(`geocoder failed: ${response.status}`)
  }
  const body = await response.json()
  const hits: GeocodeHit[] = []
  const seen = new Set<string>()
  const features = (body.features ?? []) as PhotonFeature[]
  // Rows are deduped by name+context below, keeping whichever Photon ranked
  // first — so a station can suppress the district it is named after. All
  // eight "Paddington" hits in London share one key, and only the `place`
  // one is the toponym. Claim those keys up front so it always wins the
  // row; a differently-named station ("London Paddington", "Cork Kent")
  // has its own key and is unaffected.
  const toponymKeys = new Set<string>()
  for (const feature of features) {
    const props = feature.properties
    if (props.name && props.osm_key === 'place') {
      toponymKeys.add(`${props.name}|${photonContext(props)}`)
    }
  }
  for (const feature of features) {
    const props = feature.properties
    if (!props.name) continue
    // The map's poi filter has no say here — geocoder hits never touch the
    // style — so the same rule is applied again in Photon's vocabulary.
    if (!isToponymicPhotonHit(props.osm_key ?? '', props.osm_value ?? '')) {
      continue
    }
    const context = photonContext(props)
    const key = `${props.name}|${context}`
    if (seen.has(key)) continue
    if (props.osm_key !== 'place' && toponymKeys.has(key)) continue
    seen.add(key)
    const [lng, lat] = feature.geometry.coordinates
    const extent = props.extent
    hits.push({
      name: props.name,
      kind: kindFromPhoton(props.osm_key ?? '', props.osm_value ?? ''),
      context,
      lngLat: { lng, lat },
      extent: extent
        ? [extent[0], extent[3], extent[2], extent[1]]
        : null,
      osmRef:
        props.osm_type && props.osm_id
          ? `${PHOTON_TYPE[props.osm_type] ?? props.osm_type}/${props.osm_id}`
          : null,
    })
  }
  return hits
}

/** Also plants the CSRF cookie — call once on app load, before any POST. */
export async function fetchMe(signal?: AbortSignal): Promise<Me> {
  const response = await fetch('/api/me/', { signal })
  if (!response.ok) {
    throw new Error(`me failed: ${response.status}`)
  }
  const body = await response.json()
  // Default open: an older server that doesn't send the field is one that
  // never closes signups, and guessing "closed" would hide a working form.
  return { user: body.user ?? null, signupsOpen: body.signups_open !== false }
}

export async function getPlace(
  slug: string,
  signal?: AbortSignal,
): Promise<PlaceDetail> {
  const response = await fetch(`/api/places/${slug}/`, { signal })
  if (!response.ok) {
    throw new Error(`place fetch failed: ${response.status}`)
  }
  return response.json()
}

export async function saveArticle(
  slug: string,
  content: ArticleContent,
  comment: string,
): Promise<ArticleData> {
  const response = await fetch(`/api/places/${slug}/article/`, {
    method: 'PUT',
    headers: jsonHeaders(),
    body: JSON.stringify({ content, comment }),
  })
  if (!response.ok) {
    throw new Error(`save failed: ${response.status}`)
  }
  const body = await response.json()
  return body.article
}

/** History is paginated: the server caps a page, and `has_more` says whether
 *  older revisions remain. Pass the number already loaded as `offset`. */
export async function listRevisions(
  slug: string,
  offset = 0,
  signal?: AbortSignal,
): Promise<RevisionPage> {
  const query = offset ? `?offset=${offset}` : ''
  const response = await fetch(`/api/places/${slug}/revisions/${query}`, {
    signal,
  })
  if (!response.ok) {
    throw new Error(`revisions fetch failed: ${response.status}`)
  }
  return await response.json()
}

export async function getRevision(
  slug: string,
  revisionId: number,
  signal?: AbortSignal,
): Promise<RevisionDetail> {
  const response = await fetch(
    `/api/places/${slug}/revisions/${revisionId}/`,
    { signal },
  )
  if (!response.ok) {
    throw new Error(`revision fetch failed: ${response.status}`)
  }
  const body = await response.json()
  return body.revision
}

/** Set an article's protection level (moderators only). */
export async function setProtection(
  slug: string,
  level: ProtectionLevel,
): Promise<ProtectionLevel> {
  const response = await fetch(`/api/places/${slug}/protection/`, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({ protection_level: level }),
  })
  if (!response.ok) {
    throw new Error(`protection failed: ${response.status}`)
  }
  const body = await response.json()
  return body.protection_level
}

/** Soft-delete a whole article (admin only). Every revision survives — the
 *  place just reads as a stub until a restore or the next write. */
export async function deleteArticle(
  slug: string,
  reason: string,
): Promise<void> {
  const response = await fetch(`/api/places/${slug}/delete/`, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({ reason }),
  })
  if (!response.ok) {
    throw new Error(`delete failed: ${response.status}`)
  }
}

/** Un-delete an article (admin only, inverse of deleteArticle). */
export async function restoreArticle(slug: string): Promise<ArticleData> {
  const response = await fetch(`/api/places/${slug}/restore/`, {
    method: 'POST',
    headers: jsonHeaders(),
  })
  if (!response.ok) {
    throw new Error(`restore failed: ${response.status}`)
  }
  const body = await response.json()
  return body.article
}

export async function revertArticle(
  slug: string,
  revisionId: number,
): Promise<ArticleData> {
  const response = await fetch(`/api/places/${slug}/revert/`, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({ revision_id: revisionId }),
  })
  if (!response.ok) {
    throw new Error(`revert failed: ${response.status}`)
  }
  const body = await response.json()
  return body.article
}

/** The server caps how many threads one response carries; `has_more` means
 *  the place has more discussion than is shown. */
export async function getTalk(
  slug: string,
  signal?: AbortSignal,
): Promise<TalkPage> {
  const response = await fetch(`/api/places/${slug}/talk/`, { signal })
  if (!response.ok) {
    throw new Error(`talk fetch failed: ${response.status}`)
  }
  return await response.json()
}

export async function createTalkThread(
  slug: string,
  title: string,
  bodyMd: string,
): Promise<TalkThread> {
  const response = await fetch(`/api/places/${slug}/talk/`, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({ title, body_md: bodyMd }),
  })
  if (!response.ok) {
    throw new Error(`thread create failed: ${response.status}`)
  }
  const body = await response.json()
  return body.thread
}

export async function replyTalkThread(
  threadId: number,
  bodyMd: string,
): Promise<TalkPost> {
  const response = await fetch(`/api/talk/${threadId}/posts/`, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({ body_md: bodyMd }),
  })
  if (!response.ok) {
    throw new Error(`reply failed: ${response.status}`)
  }
  const body = await response.json()
  return body.post
}

export async function editTalkPost(
  postId: number,
  bodyMd: string,
): Promise<TalkPost> {
  const response = await fetch(`/api/talk/posts/${postId}/`, {
    method: 'PUT',
    headers: jsonHeaders(),
    body: JSON.stringify({ body_md: bodyMd }),
  })
  if (!response.ok) {
    throw new Error(`post edit failed: ${response.status}`)
  }
  const body = await response.json()
  return body.post
}

/** Soft-delete a post (own post, or any post as a moderator). */
export async function deleteTalkPost(postId: number): Promise<TalkPost> {
  const response = await fetch(`/api/talk/posts/${postId}/delete/`, {
    method: 'DELETE',
    headers: jsonHeaders(),
  })
  if (!response.ok) {
    throw new Error(`post delete failed: ${response.status}`)
  }
  const body = await response.json()
  return body.post
}

/** Soft-delete a whole thread (moderators only). */
export async function deleteTalkThread(threadId: number): Promise<void> {
  const response = await fetch(`/api/talk/${threadId}/`, {
    method: 'DELETE',
    headers: jsonHeaders(),
  })
  if (!response.ok) {
    throw new Error(`thread delete failed: ${response.status}`)
  }
}

/** Flag a revision or a talk post for moderator attention. */
export async function createReport(
  targetType: 'revision' | 'talk_post',
  targetId: number,
  category: ReportCategory,
  reason: string,
): Promise<void> {
  const response = await fetch('/api/reports/', {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({
      target_type: targetType,
      target_id: targetId,
      category,
      reason,
    }),
  })
  if (!response.ok) {
    throw new Error(`report failed: ${response.status}`)
  }
}

/** The moderator queue: open reports with target context. */
export async function fetchReports(
  signal?: AbortSignal,
): Promise<ReportRow[]> {
  const response = await fetch('/api/mod/reports/', { signal })
  if (!response.ok) {
    throw new Error(`reports fetch failed: ${response.status}`)
  }
  const body = await response.json()
  return body.reports
}

/** Resolve, dismiss, delete, or suppress a report's target from the queue. */
export async function actOnReport(
  reportId: number,
  action: ReportAction,
): Promise<void> {
  const response = await fetch(`/api/mod/reports/${reportId}/action/`, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({ action }),
  })
  if (!response.ok) {
    throw new Error(`report action failed: ${response.status}`)
  }
}

// --- Moderation dashboard ---------------------------

/** Users with reports or removed content against them, most-recent first. */
export async function fetchModUsers(
  options: { all?: boolean; signal?: AbortSignal } = {},
): Promise<ModUsersResult> {
  const query = options.all ? '?all=1' : ''
  const response = await fetch(`/api/mod/users/${query}`, {
    signal: options.signal,
  })
  if (!response.ok) {
    throw new Error(`mod users fetch failed: ${response.status}`)
  }
  const data = await response.json()
  return { users: data.users, truncated: !!data.truncated }
}

/** One user's content, reports, bans, and audit trail. */
export async function fetchModUser(
  userId: number,
  signal?: AbortSignal,
): Promise<ModUserDetail> {
  const response = await fetch(`/api/mod/users/${userId}/`, { signal })
  if (!response.ok) {
    throw new Error(`mod user fetch failed: ${response.status}`)
  }
  return response.json()
}

/** Resolves with what the removal actually took down, or null when removal
 *  wasn't requested — the server distinguishes the two. */
export async function banUser(
  userId: number,
  input: BanInput,
): Promise<RemovedContent | null> {
  const response = await fetch(`/api/mod/users/${userId}/ban/`, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify(input),
  })
  if (!response.ok) {
    throw new Error(`ban failed: ${response.status}`)
  }
  const data = await response.json()
  return data.removed_content ?? null
}

export async function unbanUser(userId: number): Promise<void> {
  const response = await fetch(`/api/mod/users/${userId}/unban/`, {
    method: 'POST',
    headers: jsonHeaders(),
  })
  if (!response.ok) {
    throw new Error(`unban failed: ${response.status}`)
  }
}

/** Promote a user to moderator or demote one back (superuser only). */
export async function setUserRole(
  userId: number,
  role: 'user' | 'moderator',
): Promise<void> {
  const response = await fetch(`/api/mod/users/${userId}/role/`, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({ role }),
  })
  if (!response.ok) {
    throw new Error(`role change failed: ${response.status}`)
  }
}

/** Reporters ranked by dismissed reports — the report-abuse view. */
export async function fetchModReporters(
  signal?: AbortSignal,
): Promise<ModReporter[]> {
  const response = await fetch('/api/mod/reporters/', { signal })
  if (!response.ok) {
    throw new Error(`reporters fetch failed: ${response.status}`)
  }
  return (await response.json()).reporters
}

export async function fetchModAudit(
  offset = 0,
  filters: ModAuditFilters = {},
  signal?: AbortSignal,
): Promise<ModAuditPage> {
  const params = new URLSearchParams({ offset: String(offset) })
  if (filters.target != null) params.set('target', String(filters.target))
  // A group goes as one comma-separated param, so the server's `total` counts
  // the same rows the page shows.
  if (filters.actions?.length) params.set('action', filters.actions.join(','))
  const response = await fetch(`/api/mod/audit/?${params}`, { signal })
  if (!response.ok) {
    throw new Error(`audit fetch failed: ${response.status}`)
  }
  return response.json()
}

/** Restore a soft-deleted talk post (inverse of a queue delete). */
export async function restoreTalkPost(postId: number): Promise<void> {
  const response = await fetch(`/api/mod/talk/posts/${postId}/restore/`, {
    method: 'POST',
    headers: jsonHeaders(),
  })
  if (!response.ok) {
    throw new Error(`restore failed: ${response.status}`)
  }
}

/** Put a soft-deleted talk thread back (inverse of a thread delete).
 *  Posts removed individually inside it stay removed. */
export async function restoreTalkThread(threadId: number): Promise<void> {
  const response = await fetch(
    `/api/mod/talk/threads/${threadId}/restore/`,
    { method: 'POST', headers: jsonHeaders() },
  )
  if (!response.ok) {
    throw new Error(`restore failed: ${response.status}`)
  }
}

/** Un-suppress a revision (inverse of a queue suppress). */
export async function restoreRevision(revisionId: number): Promise<void> {
  const response = await fetch(
    `/api/mod/revisions/${revisionId}/restore/`,
    { method: 'POST', headers: jsonHeaders() },
  )
  if (!response.ok) {
    throw new Error(`restore failed: ${response.status}`)
  }
}

/** django-allauth headless errors carry { errors: [{ message, param }] };
 *  surface them as one readable string, falling back to the status code. */
async function allauthErrorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json()
    if (Array.isArray(body.errors) && body.errors.length > 0) {
      return body.errors
        .map((e: { message: string }) => e.message)
        .join(' ')
    }
  } catch {
    // non-JSON error body; fall through to the status code
  }
  // allauth's throttles answer a bare `{"status": 429}` with no errors array,
  // which would surface to the user as the string "429". The one that bites on
  // a real path is confirm_email at 1/10s/key: sign up, reload, log in again
  // straight away, and the login is refused rather than re-sending the code.
  if (response.status === 429) {
    return 'Too many attempts just now. Wait a moment and try again.'
  }
  return `${response.status}`
}

/** allauth answers **401 with a pending `verify_email` flow** whenever the
 *  credentials were accepted but the address was never confirmed — after a
 *  signup, and equally when an account that abandoned verification logs in.
 *  It is not an error either time: the code has just been emailed and the
 *  caller's job is to collect it.
 *
 *  Read the flow rather than the bare status, because a 401 alone does not
 *  mean this: any other pending login stage answers 401 too, and must not be
 *  shown an email-code box. Wrong credentials are a 400, not a 401.
 *
 *  Clone before reading — the body is a stream, and an unrecognised 401 goes
 *  on to allauthErrorMessage, which needs to read it again. */
async function emailVerificationPending(response: Response): Promise<boolean> {
  if (response.status !== 401) return false
  try {
    const body = await response.clone().json()
    const flows: { id: string; is_pending?: boolean }[] = body?.data?.flows ?? []
    return flows.some((flow) => flow.id === 'verify_email' && flow.is_pending)
  } catch {
    return false
  }
}

/** django-allauth headless browser API for calls whose only success is 2xx. */
async function allauth(
  method: string,
  path: string,
  payload?: Record<string, string>,
): Promise<void> {
  const response = await fetch(`/_allauth/browser/v1${path}`, {
    method,
    headers: jsonHeaders(),
    body: payload ? JSON.stringify(payload) : undefined,
  })
  if (!response.ok) {
    throw new Error(await allauthErrorMessage(response))
  }
}

/** One login field accepts a username or an email address, but allauth wants
 *  exactly one of its credential keys posted (sending both is an error), so
 *  the caller's single value has to be routed to one of them. "@" is the test:
 *  usernames exclude it (core/validators.py) precisely so this can't be
 *  ambiguous. */
export async function login(
  identifier: string,
  password: string,
): Promise<{ verificationRequired: boolean }> {
  const key = identifier.includes('@') ? 'email' : 'username'
  const response = await fetch('/_allauth/browser/v1/auth/login', {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({ [key]: identifier, password }),
  })
  // Not delegated to the allauth() helper: that one is for calls whose only
  // success is 2xx, and it turned this 401 into a thrown Error("401") that no
  // screen could act on — an account that abandoned verification could log in
  // and land nowhere. The password was right and a fresh code is on its way.
  if (await emailVerificationPending(response)) {
    return { verificationRequired: true }
  }
  if (!response.ok) {
    throw new Error(await allauthErrorMessage(response))
  }
  return { verificationRequired: false }
}

/** Signup requires an email. With mandatory verification allauth answers 401
 *  with a pending verify-email flow — expected, not an error: the caller then
 *  collects the emailed code and calls verifyEmail. A 200 means the session is
 *  already authenticated (verification disabled). */
export async function signup(
  username: string,
  email: string,
  password: string,
  terms: boolean,
): Promise<{ verificationRequired: boolean }> {
  const response = await fetch('/_allauth/browser/v1/auth/signup', {
    method: 'POST',
    headers: jsonHeaders(),
    // `terms` carries the Terms-of-Use agreement through as the user actually
    // left it (core/forms.py requires it). The server, not the checkbox, is
    // what makes agreement a real precondition of having an account.
    body: JSON.stringify({ username, email, password, terms }),
  })
  if (await emailVerificationPending(response)) {
    return { verificationRequired: true }
  }
  if (!response.ok) {
    throw new Error(await allauthErrorMessage(response))
  }
  return { verificationRequired: false }
}

/** Submit the emailed verification code; success authenticates the session. */
export function verifyEmail(code: string): Promise<void> {
  return allauth('POST', '/auth/email/verify', { key: code })
}

/** Ask for a password-reset code. Like signup, allauth answers 401 with a
 *  pending flow — expected, not an error. Deliberately enumeration-resistant:
 *  an address with no account gets this same response (and its own "someone
 *  asked to reset a password you don't have" mail), so the caller must never
 *  report back whether the address was found. */
export async function requestPasswordReset(email: string): Promise<void> {
  const response = await fetch(
    '/_allauth/browser/v1/auth/password/request',
    { method: 'POST', headers: jsonHeaders(), body: JSON.stringify({ email }) },
  )
  if (response.status === 401) {
    return
  }
  if (!response.ok) {
    throw new Error(await allauthErrorMessage(response))
  }
}

/** Submit the emailed code with the new password. 401 is *success* here: the
 *  reset lands but ACCOUNT_LOGIN_ON_PASSWORD_RESET is off, so the session is
 *  still anonymous and the user logs in again with the new password. A wrong
 *  code is a 400; a 409 means no reset is pending in this session — the flow
 *  is session-bound, so the code only works in the browser that asked for it. */
export async function resetPassword(
  code: string,
  password: string,
): Promise<void> {
  await allauth('POST', '/auth/password/reset', {
    key: code,
    password,
  }).catch((error: Error) => {
    if (error.message !== '401') throw error
  })
}

export async function logout(): Promise<void> {
  // allauth answers 401 ("session gone") on logout — that is success.
  await allauth('DELETE', '/auth/session').catch((error: Error) => {
    if (error.message !== '401') throw error
  })
}

/** Change the password of the signed-in account. Unlike the reset flow this
 *  needs no email round trip — proving knowledge of the current password is
 *  stronger evidence than possession of a mailbox, and the session is already
 *  authenticated. */
export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  return allauth('POST', '/account/password/change', {
    current_password: currentPassword,
    new_password: newPassword,
  })
}

/** Start an email change. Verification is by code, so nothing is stored yet:
 *  allauth only sends the message. The caller collects the code and calls
 *  verifyEmail, after which the new address *replaces* the old one
 *  (ACCOUNT_CHANGE_EMAIL). */
export function requestEmailChange(email: string): Promise<void> {
  return allauth('POST', '/account/email', { email })
}

/** Close the signed-in account. An account with no contributions is deleted;
 *  one with contributions is anonymized to a `[deleted-…]` username, since the
 *  revision history is the site's attribution mechanism and cannot lose its
 *  rows. Either way the session ends. See server/core/accounts.py. */
export async function closeAccount(
  password: string,
): Promise<{ outcome: 'deleted' | 'anonymized'; username: string | null }> {
  const response = await fetch('/api/account/close/', {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({ password }),
  })
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      error?: string
    } | null
    throw new Error(body?.error ?? 'Could not close the account.')
  }
  return response.json()
}
