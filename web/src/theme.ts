export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'toponymia:theme'
const DARK_QUERY = '(prefers-color-scheme: dark)'

/** The explicit choice this browser has stored, or `null` for "never asked".
 *
 *  The null is the point: "no choice yet" has to stay distinguishable from
 *  "chose light", because only the first one follows the OS. Anything
 *  unrecognised in storage counts as no choice. */
export function storedTheme(): Theme | null {
  const stored = localStorage.getItem(STORAGE_KEY)
  return stored === 'dark' || stored === 'light' ? stored : null
}

/** What the reader's OS/browser asks for right now. */
export function systemTheme(): Theme {
  return window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light'
}

/** The theme to open with: a stored choice, else the system preference.
 *
 *  Kept in sync with the anti-flash script in index.html, which makes the
 *  same decision before this bundle parses. */
export function initialTheme(): Theme {
  return storedTheme() ?? systemTheme()
}

export function storeTheme(theme: Theme) {
  localStorage.setItem(STORAGE_KEY, theme)
}

/** Follow the OS while no explicit choice is stored.
 *
 *  Someone who switches their desktop to dark at sunset expects the tab
 *  they left open to come with it. The stored-choice check is inside the
 *  listener rather than around the subscription so that it reflects the
 *  choice at the moment the OS changes, not at the moment we subscribed.
 *
 *  Returns an unsubscribe. */
export function watchSystemTheme(onChange: (theme: Theme) => void) {
  const media = window.matchMedia(DARK_QUERY)
  const handle = (event: MediaQueryListEvent) => {
    if (storedTheme() === null) onChange(event.matches ? 'dark' : 'light')
  }
  media.addEventListener('change', handle)
  return () => media.removeEventListener('change', handle)
}

/** The stylesheet keys off `data-theme` on <html>. Kept in one place
 *  because index.html sets the same attribute before the bundle loads —
 *  see the anti-flash script there. */
export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme
}
