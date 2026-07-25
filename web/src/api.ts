import type { FeatureCollection, Geometry } from 'geojson'
import type {
  ArticleContent,
  ArticleData,
  ClickContext,
  GeocodeHit,
  PlaceDetail,
  BanInput,
  ModAuditRow,
  ModReporter,
  ModUserDetail,
  ModUsersResult,
  ProtectionLevel,
  ReportAction,
  ReportCategory,
  ReportRow,
  ResolvedPlace,
  ResolveResponse,
  RevisionDetail,
  RevisionSummary,
  SearchResult,
  TalkPost,
  TalkThread,
  User,
} from './types'

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
 *  own rate limit, `failed` = anything else. */
export type ResolveFailure = 'unavailable' | 'throttled' | 'failed'

export class ResolveError extends Error {
  readonly reason: ResolveFailure

  constructor(status: number) {
    super(`resolve failed: ${status}`)
    this.name = 'ResolveError'
    this.reason =
      status === 503 ? 'unavailable' : status === 429 ? 'throttled' : 'failed'
  }
}

export async function resolveFeature(
  name: string,
  kind: string,
  click: ClickContext,
  nameEn: string | null,
  signal?: AbortSignal,
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

/** A random place with an article, or null while the wiki is empty. */
export async function fetchRandomArticle(): Promise<ResolvedPlace | null> {
  const response = await fetch('/api/random/')
  if (!response.ok) {
    throw new Error(`random failed: ${response.status}`)
  }
  const body = await response.json()
  return body.place
}

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

/** Geocoder half of search (Photon — public, keyless, CORS-open).
 * Biased toward the current map view when a center is given. */
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
  for (const feature of (body.features ?? []) as PhotonFeature[]) {
    const props = feature.properties
    if (!props.name) continue
    const context = [props.city, props.state, props.country]
      .filter((part): part is string => !!part && part !== props.name)
      .join(', ')
    const key = `${props.name}|${context}`
    if (seen.has(key)) continue
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
export async function fetchMe(signal?: AbortSignal): Promise<User | null> {
  const response = await fetch('/api/me/', { signal })
  if (!response.ok) {
    throw new Error(`me failed: ${response.status}`)
  }
  const body = await response.json()
  return body.user
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

export async function listRevisions(
  slug: string,
  signal?: AbortSignal,
): Promise<RevisionSummary[]> {
  const response = await fetch(`/api/places/${slug}/revisions/`, { signal })
  if (!response.ok) {
    throw new Error(`revisions fetch failed: ${response.status}`)
  }
  const body = await response.json()
  return body.revisions
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

export async function getTalk(
  slug: string,
  signal?: AbortSignal,
): Promise<TalkThread[]> {
  const response = await fetch(`/api/places/${slug}/talk/`, { signal })
  if (!response.ok) {
    throw new Error(`talk fetch failed: ${response.status}`)
  }
  const body = await response.json()
  return body.threads
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

// --- Moderation dashboard (DESIGN.md M12) ---------------------------

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

export async function banUser(
  userId: number,
  input: BanInput,
): Promise<void> {
  const response = await fetch(`/api/mod/users/${userId}/ban/`, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify(input),
  })
  if (!response.ok) {
    throw new Error(`ban failed: ${response.status}`)
  }
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
  signal?: AbortSignal,
): Promise<ModAuditRow[]> {
  const response = await fetch('/api/mod/audit/', { signal })
  if (!response.ok) {
    throw new Error(`audit fetch failed: ${response.status}`)
  }
  return (await response.json()).actions
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

/** django-allauth headless browser API. Errors carry
 * { errors: [{ message, param }] } — surfaced as a readable string. */
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
    let detail = `${response.status}`
    try {
      const body = await response.json()
      if (Array.isArray(body.errors) && body.errors.length > 0) {
        detail = body.errors
          .map((e: { message: string }) => e.message)
          .join(' ')
      }
    } catch {
      // non-JSON error body; keep the status code
    }
    throw new Error(detail)
  }
}

export function login(username: string, password: string): Promise<void> {
  return allauth('POST', '/auth/login', { username, password })
}

export function signup(username: string, password: string): Promise<void> {
  return allauth('POST', '/auth/signup', { username, password })
}

export async function logout(): Promise<void> {
  // allauth answers 401 ("session gone") on logout — that is success.
  await allauth('DELETE', '/auth/session').catch((error: Error) => {
    if (error.message !== '401') throw error
  })
}
