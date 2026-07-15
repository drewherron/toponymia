/** Revision comparison (DESIGN.md §6): body Markdown is diffed as text
 * into side-by-side rows, structured fields (names, derivations,
 * see_also) are diffed field-wise. Rendering lives in HistoryTab. */

import { diffLines, diffWordsWithSpace } from 'diff'
import type { ArticleContent, Derivation, NameEntry } from './types'

export interface DiffSpan {
  text: string
  kind: 'same' | 'add' | 'del'
}

/** One side-by-side row; a null side means the line only exists on the
 * other side (pure insertion/deletion). */
export interface DiffRow {
  left: DiffSpan[] | null
  right: DiffSpan[] | null
  changed: boolean
}

function toLines(value: string): string[] {
  const lines = value.split('\n')
  // diffLines values end with '\n' except possibly the last chunk;
  // splitting leaves a trailing '' to drop.
  if (lines[lines.length - 1] === '') lines.pop()
  return lines
}

/** Word-level spans for a pair of changed lines. */
function wordSpans(
  oldLine: string,
  newLine: string,
): { left: DiffSpan[]; right: DiffSpan[] } {
  const left: DiffSpan[] = []
  const right: DiffSpan[] = []
  for (const part of diffWordsWithSpace(oldLine, newLine)) {
    if (part.added) {
      right.push({ text: part.value, kind: 'add' })
    } else if (part.removed) {
      left.push({ text: part.value, kind: 'del' })
    } else {
      left.push({ text: part.value, kind: 'same' })
      right.push({ text: part.value, kind: 'same' })
    }
  }
  return { left, right }
}

export function diffBody(oldText: string, newText: string): DiffRow[] {
  const rows: DiffRow[] = []
  let removed: string[] = []
  let added: string[] = []

  const flush = () => {
    const count = Math.max(removed.length, added.length)
    for (let i = 0; i < count; i++) {
      const oldLine = i < removed.length ? removed[i] : null
      const newLine = i < added.length ? added[i] : null
      if (oldLine !== null && newLine !== null) {
        const { left, right } = wordSpans(oldLine, newLine)
        rows.push({ left, right, changed: true })
      } else {
        rows.push({
          left:
            oldLine !== null ? [{ text: oldLine, kind: 'del' }] : null,
          right:
            newLine !== null ? [{ text: newLine, kind: 'add' }] : null,
          changed: true,
        })
      }
    }
    removed = []
    added = []
  }

  for (const part of diffLines(oldText, newText)) {
    if (part.removed) {
      removed.push(...toLines(part.value))
    } else if (part.added) {
      added.push(...toLines(part.value))
    } else {
      flush()
      for (const line of toLines(part.value)) {
        const span: DiffSpan[] = [{ text: line, kind: 'same' }]
        rows.push({ left: span, right: span, changed: false })
      }
    }
  }
  flush()
  return rows
}

export interface FieldChange {
  label: string
  kind: 'added' | 'removed' | 'changed'
  /** Present for 'changed' (and as the sole side for added/removed). */
  old?: string
  new?: string
}

function nameKey(entry: NameEntry): string {
  return entry.language ? `${entry.name} (${entry.language})` : entry.name
}

function nameFields(entry: NameEntry): Record<string, string> {
  return {
    endonym: entry.is_endonym ? 'yes' : 'no',
    'from languages': entry.from_languages.join(', '),
    etymology: entry.etymology_md,
    references: entry.references.join('\n'),
  }
}

function derivationText(d: Derivation): string {
  return [d.term, d.note, d.url].filter(Boolean).join(' — ')
}

/** Everything except body_md, compared field-wise. Name entries are
 * matched by name+language; a renamed entry reads as removed+added. */
export function diffStructured(
  oldContent: ArticleContent,
  newContent: ArticleContent,
): FieldChange[] {
  const changes: FieldChange[] = []

  const oldNames = new Map(oldContent.names.map((n) => [nameKey(n), n]))
  const newNames = new Map(newContent.names.map((n) => [nameKey(n), n]))
  for (const [key, oldEntry] of oldNames) {
    const newEntry = newNames.get(key)
    if (!newEntry) {
      changes.push({ label: `name ${key}`, kind: 'removed', old: key })
      continue
    }
    const before = nameFields(oldEntry)
    const after = nameFields(newEntry)
    for (const field of Object.keys(before)) {
      if (before[field] !== after[field]) {
        changes.push({
          label: `${key} · ${field}`,
          kind: 'changed',
          old: before[field],
          new: after[field],
        })
      }
    }
  }
  for (const key of newNames.keys()) {
    if (!oldNames.has(key)) {
      changes.push({ label: `name ${key}`, kind: 'added', new: key })
    }
  }

  const oldDerivations = new Map(
    oldContent.derivations.map((d) => [d.term, derivationText(d)]),
  )
  const newDerivations = new Map(
    newContent.derivations.map((d) => [d.term, derivationText(d)]),
  )
  for (const [term, text] of oldDerivations) {
    const after = newDerivations.get(term)
    if (after === undefined) {
      changes.push({ label: `derivation ${term}`, kind: 'removed', old: text })
    } else if (after !== text) {
      changes.push({
        label: `derivation ${term}`,
        kind: 'changed',
        old: text,
        new: after,
      })
    }
  }
  for (const [term, text] of newDerivations) {
    if (!oldDerivations.has(term)) {
      changes.push({ label: `derivation ${term}`, kind: 'added', new: text })
    }
  }

  const oldSeeAlso = new Set(oldContent.see_also)
  const newSeeAlso = new Set(newContent.see_also)
  for (const item of oldSeeAlso) {
    if (!newSeeAlso.has(item)) {
      changes.push({ label: `see also ${item}`, kind: 'removed', old: item })
    }
  }
  for (const item of newSeeAlso) {
    if (!oldSeeAlso.has(item)) {
      changes.push({ label: `see also ${item}`, kind: 'added', new: item })
    }
  }

  return changes
}
