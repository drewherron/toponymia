import { useEffect, useState } from 'react'

/** Below this the article pane becomes a bottom sheet and the header collapses. */
export const NARROW_QUERY = '(max-width: 768px)'
/** Touch (or pen): tap targets need a fingertip-sized query box, not a mouse one. */
export const COARSE_QUERY = '(pointer: coarse)'

// Pane geometry lives here rather than in CSS because the map's camera padding
// has to agree with it exactly — a place flown to while the pane covers part of
// the canvas must land in the part that's still visible. One source of truth,
// read by both FeaturePane (which sizes itself) and MapView (which pads).
export const PANE_WIDTH = 560
export const PANE_WIDE_WIDTH = 760
/** The pane never eats the whole map on an in-between width. */
export const PANE_MAX_VW = 0.9

export type SheetDetent = 'peek' | 'half' | 'full'

/** Enough for the title row and the tab bar — the sheet's "get out of the way"
 *  position, where you're browsing the map with an article parked below. */
export const SHEET_PEEK_PX = 148
/** Fraction of the map area each detent covers; peek is fixed px instead. */
export const SHEET_FRACTION: Record<SheetDetent, number | null> = {
  peek: null,
  half: 0.5,
  full: 0.92,
}
export const SHEET_DETENTS: SheetDetent[] = ['peek', 'half', 'full']

/** Sheet height in px for a given map-area height. */
export function sheetHeight(detent: SheetDetent, areaHeight: number): number {
  const fraction = SHEET_FRACTION[detent]
  return fraction == null
    ? SHEET_PEEK_PX
    : Math.round(areaHeight * fraction)
}

/** CSS height for a detent — percentages resolve against the map area, so the
 *  sheet and sheetHeight() agree without the pane measuring anything. */
export function sheetCssHeight(detent: SheetDetent): string {
  const fraction = SHEET_FRACTION[detent]
  return fraction == null ? `${SHEET_PEEK_PX}px` : `${fraction * 100}%`
}

/** Live match for a media query. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () => window.matchMedia(query).matches,
  )
  useEffect(() => {
    const list = window.matchMedia(query)
    const onChange = () => setMatches(list.matches)
    onChange() // the query may have changed between render and effect
    list.addEventListener('change', onChange)
    return () => list.removeEventListener('change', onChange)
  }, [query])
  return matches
}
