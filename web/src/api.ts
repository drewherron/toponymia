import type { FeatureCollection } from 'geojson'
import type {
  ArticleContent,
  ArticleData,
  ClickContext,
  PlaceDetail,
  ResolveResponse,
  RevisionDetail,
  RevisionSummary,
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

export async function resolveFeature(
  name: string,
  kind: string,
  click: ClickContext,
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
    }),
  })
  if (!response.ok) {
    throw new Error(`resolve failed: ${response.status}`)
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
