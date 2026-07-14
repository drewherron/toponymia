import { useState } from 'react'
import type { FormEvent } from 'react'
import { saveArticle } from '../api'
import type { ArticleContent, ArticleData, NameEntry } from '../types'

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
  const [body, setBody] = useState(initial?.body_md ?? '')
  const [names, setNames] = useState<NameDraft[]>(
    initial && initial.names.length > 0
      ? initial.names.map(toDraft)
      : [emptyDraft(displayName)],
  )
  const [comment, setComment] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const updateName = (index: number, patch: Partial<NameDraft>) => {
    setNames((prev) =>
      prev.map((draft, i) => (i === index ? { ...draft, ...patch } : draft)),
    )
  }

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    const content: ArticleContent = {
      body_md: body,
      names: names.map(fromDraft).filter((entry) => entry.name),
      // not editable here yet — carried through from the last revision
      derivations: initial?.derivations ?? [],
      see_also: initial?.see_also ?? [],
    }
    if (!content.body_md.trim() && content.names.length === 0) {
      setError('Write a body or add at least one name.')
      return
    }
    setBusy(true)
    setError(null)
    saveArticle(slug, content, comment.trim())
      .then(onSaved)
      .catch(() => {
        setError('Could not save the article. Are you still logged in?')
        setBusy(false)
      })
  }

  return (
    <form className="article-editor" onSubmit={handleSubmit}>
      <label>
        Article body (Markdown)
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={10}
          placeholder="What does this place name mean? Where does it come from?"
        />
      </label>

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
            Derived from languages (comma-separated codes)
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
              rows={4}
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

      {error && <p className="article-editor-error">{error}</p>}
      <div className="article-editor-actions">
        <button type="submit" disabled={busy}>
          {busy ? 'Saving…' : 'Save'}
        </button>
        <button type="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  )
}

export default ArticleEditor
