import type { ClickContext, ResolveResponse } from './types'

export async function resolveFeature(
  name: string,
  kind: string,
  click: ClickContext,
  signal?: AbortSignal,
): Promise<ResolveResponse> {
  const response = await fetch('/api/resolve/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
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
