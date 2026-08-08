/** Revision comparison: body Markdown is diffed as text
 * into side-by-side rows, structured fields (names, their etymologies,
 * derivations, see_also) are diffed field-wise. Rendering lives in
 * HistoryTab. */

import { diffLines, diffWordsWithSpace } from 'diff'
import type {
  ArticleContent,
  Derivation,
  Element,
  Etymology,
  NameEntry,
} from './types'

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

/** A final line without a trailing newline diffs as a different token
 * than the same line with one — normalize so only real edits show. */
function withEofNewline(text: string): string {
  return text === '' || text.endsWith('\n') ? text : `${text}\n`
}

export function diffBody(oldText: string, newText: string): DiffRow[] {
  oldText = withEofNewline(oldText)
  newText = withEofNewline(newText)
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
  }
}

function elementText(element: Element): string {
  const parts = [element.form]
  if (element.language) parts.push(`[${element.language}]`)
  if (element.gloss) parts.push(`‘${element.gloss}’`)
  if (element.role) parts.push(`(${element.role})`)
  return parts.join(' ')
}

function etymologyFields(entry: Etymology): Record<string, string> {
  return {
    confidence: entry.confidence,
    'from languages': entry.from_languages.join(', '),
    elements: entry.elements.map(elementText).join('\n'),
    etymology: entry.etymology_md,
    references: entry.references.join('\n'),
  }
}

/** A whole etymology as one blob, for when it is added or removed
 *  outright rather than edited field by field. */
function etymologyText(entry: Etymology): string {
  const fields = etymologyFields(entry)
  return Object.keys(fields)
    .filter((field) => fields[field] !== '')
    .map((field) => `${field}: ${fields[field]}`)
    .join('\n')
}

/** Compare one name's hypotheses position-wise.
 *
 *  Index rather than content: an etymology has no stable identity, and
 *  order is editorial (first = primary), so a reordering *is* a change
 *  worth showing. The index is left out of the labels when both sides
 *  have exactly one, so ordinary single-etymology diffs read exactly as
 *  they did before competing hypotheses existed. */
function diffEtymologies(
  key: string,
  oldList: Etymology[],
  newList: Etymology[],
): FieldChange[] {
  const changes: FieldChange[] = []
  const single = oldList.length === 1 && newList.length === 1
  const count = Math.max(oldList.length, newList.length)
  for (let i = 0; i < count; i++) {
    const scope = single ? '' : `etymology ${i + 1} · `
    const oldEntry = oldList[i]
    const newEntry = newList[i]
    if (!newEntry) {
      changes.push({
        label: `${key} · etymology ${i + 1}`,
        kind: 'removed',
        old: etymologyText(oldEntry),
      })
      continue
    }
    if (!oldEntry) {
      changes.push({
        label: `${key} · etymology ${i + 1}`,
        kind: 'added',
        new: etymologyText(newEntry),
      })
      continue
    }
    const before = etymologyFields(oldEntry)
    const after = etymologyFields(newEntry)
    for (const field of Object.keys(before)) {
      if (before[field] !== after[field]) {
        changes.push({
          label: `${key} · ${scope}${field}`,
          kind: 'changed',
          old: before[field],
          new: after[field],
        })
      }
    }
  }
  return changes
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
    changes.push(
      ...diffEtymologies(key, oldEntry.etymologies, newEntry.etymologies),
    )
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
