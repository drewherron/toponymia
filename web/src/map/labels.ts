import type { ExpressionSpecification } from 'maplibre-gl'

/** Map label languages (OpenMapTiles `name:*` codes), labeled in their
 *  own language. English first; the rest alphabetical by code. */
export const LABEL_LANGUAGES: { code: string; label: string }[] = [
  { code: 'en', label: 'English' },
  { code: 'ar', label: 'العربية' },
  { code: 'de', label: 'Deutsch' },
  { code: 'el', label: 'Ελληνικά' },
  { code: 'es', label: 'Español' },
  { code: 'fr', label: 'Français' },
  { code: 'he', label: 'עברית' },
  { code: 'hi', label: 'हिन्दी' },
  { code: 'it', label: 'Italiano' },
  { code: 'ja', label: '日本語' },
  { code: 'ko', label: '한국어' },
  { code: 'nl', label: 'Nederlands' },
  { code: 'pl', label: 'Polski' },
  { code: 'pt', label: 'Português' },
  { code: 'ru', label: 'Русский' },
  { code: 'tr', label: 'Türkçe' },
  { code: 'uk', label: 'Українська' },
  { code: 'zh', label: '中文' },
]

const STORAGE_KEY = 'toponymia:labelLanguage'

export function storedLabelLanguage(): string {
  const code = localStorage.getItem(STORAGE_KEY)
  return LABEL_LANGUAGES.some((lang) => lang.code === code) && code
    ? code
    : 'en'
}

export function storeLabelLanguage(code: string) {
  localStorage.setItem(STORAGE_KEY, code)
}

/** Property keys a label's text may come from, displayed-first.
 *  English gets the tile's legacy `name_en` too; every ladder ends on
 *  the transliterated latin name, then the native name. */
export function nameKeys(lang: string): string[] {
  return lang === 'en'
    ? ['name:en', 'name_en', 'name:latin', 'name']
    : [`name:${lang}`, 'name:latin', 'name']
}

/** The text-field expression rendering labels in `lang`. */
export function nameField(lang: string): ExpressionSpecification {
  const gets = nameKeys(lang).map(
    (key): ExpressionSpecification => ['get', key],
  )
  return ['coalesce', ...gets]
}

/** Same ladder for match expressions, where a null needle would error. */
export function nameMatch(lang: string): ExpressionSpecification {
  const gets = nameKeys(lang).map(
    (key): ExpressionSpecification => ['get', key],
  )
  return ['coalesce', ...gets, '']
}

/** The raw OSM `name`, matched alongside the display ladders so an article
 *  named for the underlying tag lights up even when the label shows an
 *  exonym. */
const RAW_NAME_MATCH: ExpressionSpecification = ['coalesce', ['get', 'name'], '']

/**
 * Does this label belong to an article? Builds `name|class` tokens from
 * the display-language name, the raw `name`, and (when relabeled) the
 * English name, and tests membership against the article token set.
 * Gating on class is what stops the city "Mexico" article from lighting
 * the country "Mexico" label — they share a name but not a `feature_class`
 * (DESIGN §2.2). `classExpr` is the label feature's class: `['get','class']`
 * for the mixed-class place layer, a constant kind for fixed-kind layers.
 */
export function tokenMatches(
  lang: string,
  classExpr: ExpressionSpecification | string,
  tokens: string[],
): ExpressionSpecification {
  const needles = [nameMatch(lang), RAW_NAME_MATCH]
  if (lang !== 'en') needles.push(nameMatch('en'))
  const tests = needles.map(
    (needle): ExpressionSpecification => [
      'in',
      ['concat', needle, '|', classExpr],
      ['literal', tokens],
    ],
  )
  return ['any', ...tests]
}

/** What the map label shows for these feature properties in `lang`. */
export function displayNameOf(
  props: Record<string, unknown>,
  lang: string,
): string | null {
  for (const key of nameKeys(lang)) {
    const value = props[key]
    if (typeof value === 'string' && value) return value
  }
  return null
}
