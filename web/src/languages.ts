/** ISO 639-3 language codes for etymology fields.
 *
 * The full table (~7,900 entries) is lazy-loaded from the `iso-639-3`
 * package the first time it's needed — the editor's save-time validation
 * and the help dialog's searchable list. The server validates against a
 * JSON generated from the same package (server/core/data/iso639_3.json),
 * so client and server always agree. The four special placeholder codes
 * (mis/mul/und/zxx) are dropped: a blank field already means "unknown".
 */

import { useEffect, useState } from 'react'

export interface LanguageEntry {
  code: string
  name: string
}

export interface LanguageTable {
  list: LanguageEntry[]
  /** ISO 639-3 code -> reference name */
  names: Map<string, string>
  /** ISO 639-1 two-letter alias -> 639-3 code */
  iso1: Map<string, string>
}

let tablePromise: Promise<LanguageTable> | null = null

export function loadLanguages(): Promise<LanguageTable> {
  tablePromise ??= import('iso-639-3').then(({ iso6393 }) => {
    const names = new Map<string, string>()
    const iso1 = new Map<string, string>()
    const list: LanguageEntry[] = []
    for (const lang of iso6393) {
      if (lang.type === 'special') continue
      names.set(lang.iso6393, lang.name)
      if (lang.iso6391) iso1.set(lang.iso6391, lang.iso6393)
      list.push({ code: lang.iso6393, name: lang.name })
    }
    return { list, names, iso1 }
  })
  return tablePromise
}

/** Canonical ISO 639-3 code for user input (639-1 aliases mapped,
 * e.g. fr -> fra), or null if unknown. Blank input stays blank. */
export function normalizeCode(
  input: string,
  table: LanguageTable,
): string | null {
  const raw = input.trim().toLowerCase()
  if (!raw) return ''
  const code = table.iso1.get(raw) ?? raw
  return table.names.has(code) ? code : null
}

/** Code -> reference name map for spelling codes out on hover, or null
 * until it has loaded. `enabled` false skips the import entirely, so an
 * article with no language codes never pulls the ~7,900-entry table. */
export function useLanguageNames(enabled: boolean): Map<string, string> | null {
  const [names, setNames] = useState<Map<string, string> | null>(null)
  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    loadLanguages().then((table) => {
      if (!cancelled) setNames(table.names)
    })
    return () => {
      cancelled = true
    }
  }, [enabled])
  return names
}
