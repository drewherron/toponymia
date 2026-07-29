export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'toponymia:theme'

/** Light for everyone until they ask otherwise.
 *
 *  Deliberately does not consult `prefers-color-scheme`: the site mirrors
 *  the light look of a print reference work, and dark is an opt-in lens
 *  rather than an OS-driven default. Anything unrecognised in storage
 *  falls back to light. */
export function storedTheme(): Theme {
  return localStorage.getItem(STORAGE_KEY) === 'dark' ? 'dark' : 'light'
}

export function storeTheme(theme: Theme) {
  localStorage.setItem(STORAGE_KEY, theme)
}

/** The stylesheet keys off `data-theme` on <html>. Kept in one place
 *  because index.html sets the same attribute before the bundle loads —
 *  see the anti-flash script there. */
export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme
}
