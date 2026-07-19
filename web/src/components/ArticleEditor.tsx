import { useState } from 'react'
import type { FormEvent } from 'react'
import { saveArticle } from '../api'
import { loadLanguages, normalizeCode } from '../languages'
import type { ArticleContent, ArticleData, NameEntry } from '../types'
import LanguageHelpDialog from './LanguageHelpDialog'

interface ArticleEditorProps {
  slug: string
  displayName: string
  initial: ArticleContent | null
  onSaved: (article: ArticleData) => void
  onCancel: () => void
}

/** Form-local shape: list fields flattened to text for editing. */
interface NameDraft {
  name: string
  language: string
  fromLanguages: string
  isEndonym: boolean
  etymology: string
  references: string
}

function toDraft(entry: NameEntry): NameDraft {
  return {
    name: entry.name,
    language: entry.language,
    fromLanguages: entry.from_languages.join(', '),
    isEndonym: entry.is_endonym,
    etymology: entry.etymology_md,
    references: entry.references.join('\n'),
  }
}

function emptyDraft(name = ''): NameDraft {
  return {
    name,
    language: '',
    fromLanguages: '',
    isEndonym: false,
    etymology: '',
    references: '',
  }
}

function fromDraft(draft: NameDraft): NameEntry {
  return {
    name: draft.name.trim(),
    language: draft.language.trim(),
    from_languages: draft.fromLanguages
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
    is_endonym: draft.isEndonym,
    etymology_md: draft.etymology,
    references: draft.references
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean),
  }
}

function ArticleEditor({
  slug,
  displayName,
  initial,
  onSaved,
  onCancel,
}: ArticleEditorProps) {
  const [names, setNames] = useState<NameDraft[]>(
    initial && initial.names.length > 0
      ? initial.names.map(toDraft)
      : [emptyDraft(displayName)],
  )
  const [comment, setComment] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)

  const updateName = (index: number, patch: Partial<NameDraft>) => {
    setNames((prev) =>
      prev.map((draft, i) => (i === index ? { ...draft, ...patch } : draft)),
    )
  }

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    const entries = names.map(fromDraft).filter((entry) => entry.name)
    if (entries.length === 0) {
      setError('Add at least one name.')
      return
    }
    setBusy(true)
    setError(null)
    // If the code table chunk fails to load, save unvalidated — the
    // server checks the same table.
    loadLanguages()
      .catch(() => null)
      .then((table) => {
        let checked = entries
        if (table) {
          const bad = new Set<string>()
          checked = entries.map((entry) => {
            const language = normalizeCode(entry.language, table)
            if (language === null) bad.add(entry.language.trim())
            const from_languages = entry.from_languages.map((code) => {
              const normal = normalizeCode(code, table)
              if (normal === null) bad.add(code)
              return normal ?? code
            })
            return { ...entry, language: language ?? entry.language, from_languages }
          })
          if (bad.size > 0) {
            setError(
              `Unknown language code${bad.size > 1 ? 's' : ''}: ` +
                `${[...bad].join(', ')}. Codes are ISO 639-3 — ` +
                'click the ? by "Derived from languages" for the list.',
            )
            setBusy(false)
            return
          }
        }
        const content: ArticleContent = {
          // free-form body removed from the UI: saving empties any legacy
          // body (a normal revision — history keeps it, revert restores it)
          body_md: '',
          names: checked,
          // not editable here yet — carried through from the last revision
          derivations: initial?.derivations ?? [],
          see_also: initial?.see_also ?? [],
        }
        saveArticle(slug, content, comment.trim())
          .then(onSaved)
          .catch(() => {
            setError('Could not save the article. Are you still logged in?')
            setBusy(false)
          })
      })
  }

  return (
    <form className="article-editor" onSubmit={handleSubmit}>
      <h2>Names</h2>
      {names.map((draft, index) => (
        <fieldset className="name-editor" key={index}>
          <div className="name-editor-row">
            <label>
              Name
              <input
                value={draft.name}
                onChange={(e) => updateName(index, { name: e.target.value })}
              />
            </label>
            <label className="name-editor-lang">
              Language
              <input
                value={draft.language}
                onChange={(e) =>
                  updateName(index, { language: e.target.value })
                }
                placeholder="eng"
              />
            </label>
          </div>
          <label className="name-editor-check">
            <input
              type="checkbox"
              checked={draft.isEndonym}
              onChange={(e) =>
                updateName(index, { isEndonym: e.target.checked })
              }
            />
            Endonym (local name)
          </label>
          <label>
            <span className="label-with-help">
              Derived from languages
              <button
                type="button"
                className="lang-help-button"
                aria-label="How to choose language codes"
                onClick={(e) => {
                  // keep the label from focusing the input
                  e.preventDefault()
                  setHelpOpen(true)
                }}
              >
                ?
              </button>
            </span>
            <input
              value={draft.fromLanguages}
              onChange={(e) =>
                updateName(index, { fromLanguages: e.target.value })
              }
              placeholder="oji, fra"
            />
          </label>
          <label>
            Etymology (Markdown)
            <textarea
              value={draft.etymology}
              onChange={(e) =>
                updateName(index, { etymology: e.target.value })
              }
              rows={6}
              placeholder={
                index === 0
                  ? 'What does this name mean? Where does it come from?'
                  : undefined
              }
            />
          </label>
          <label>
            References (one per line)
            <textarea
              value={draft.references}
              onChange={(e) =>
                updateName(index, { references: e.target.value })
              }
              rows={2}
            />
          </label>
          {names.length > 1 && (
            <button
              type="button"
              className="name-editor-remove"
              onClick={() =>
                setNames((prev) => prev.filter((_, i) => i !== index))
              }
            >
              Remove name
            </button>
          )}
        </fieldset>
      ))}
      <button
        type="button"
        className="article-editor-add"
        onClick={() => setNames((prev) => [...prev, emptyDraft()])}
      >
        + Add name
      </button>

      <label>
        Edit summary
        <input
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="Briefly describe your change (recommended)"
          maxLength={255}
        />
      </label>

      {helpOpen && <LanguageHelpDialog onClose={() => setHelpOpen(false)} />}
      {error && <p className="article-editor-error">{error}</p>}
      <div className="article-editor-actions">
        <button type="submit" disabled={busy}>
          {busy ? 'Saving…' : 'Save'}
        </button>
        <button type="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
      <p className="article-editor-note">
        By saving, you license your contribution under{' '}
        <a
          href="https://creativecommons.org/licenses/by-sa/4.0/"
          target="_blank"
          rel="noreferrer"
        >
          CC BY-SA 4.0
        </a>{' '}
        and confirm it is your own words or material you are free to use.
        Don’t paste in copyrighted text — summarize content in your own words
        and cite your sources. Citing a source is not permission to copy its
        wording.
      </p>
    </form>
  )
}

export default ArticleEditor
