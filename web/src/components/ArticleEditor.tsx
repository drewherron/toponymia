import { useState } from 'react'
import type { FormEvent } from 'react'
import { saveArticle } from '../api'
import { loadLanguages, normalizeCode } from '../languages'
import type {
  ArticleContent,
  ArticleData,
  Confidence,
  Element,
  ElementRole,
  Etymology,
  NameEntry,
} from '../types'
import { etymologyHeading } from '../etymology'
import LanguageHelpDialog from './LanguageHelpDialog'
import MarkdownHelpDialog from './MarkdownHelpDialog'
import DocumentDialog from './DocumentDialog'
import type { LegalDoc } from '../legal'

interface ArticleEditorProps {
  slug: string
  displayName: string
  initial: ArticleContent | null
  onSaved: (article: ArticleData) => void
  onCancel: () => void
}

const CONFIDENCE_OPTIONS: { value: Confidence; label: string }[] = [
  { value: '', label: '— not specified —' },
  { value: 'attested', label: 'Attested' },
  { value: 'probable', label: 'Probable' },
  { value: 'proposed', label: 'Proposed' },
  { value: 'disputed', label: 'Disputed' },
  { value: 'folk', label: 'Folk etymology' },
  { value: 'unknown', label: 'Origin unknown' },
]

const ROLE_OPTIONS: { value: ElementRole; label: string }[] = [
  { value: '', label: '—' },
  { value: 'generic', label: 'Generic' },
  { value: 'specific', label: 'Specific' },
  { value: 'affix', label: 'Affix' },
  { value: 'connective', label: 'Connective' },
]

/** Form-local shape: list fields flattened to text for editing.
 *
 *  `script` and `transliteration` have no inputs — six fields per element
 *  row is more form than the feature is worth — but they ride along so
 *  that editing an article that has them (a bot import, say) doesn't
 *  silently strip them on save. */
interface ElementDraft {
  form: string
  language: string
  gloss: string
  role: ElementRole
  script: string
  transliteration: string
}

interface EtymologyDraft {
  etymology: string
  confidence: Confidence
  fromLanguages: string
  elements: ElementDraft[]
  references: string
}

interface NameDraft {
  name: string
  language: string
  isEndonym: boolean
  etymologies: EtymologyDraft[]
}

function emptyElement(): ElementDraft {
  return {
    form: '',
    language: '',
    gloss: '',
    role: '',
    script: '',
    transliteration: '',
  }
}

function toElementDraft(element: Element): ElementDraft {
  return {
    form: element.form,
    language: element.language,
    gloss: element.gloss,
    role: element.role,
    script: element.script,
    transliteration: element.transliteration,
  }
}

function emptyEtymology(): EtymologyDraft {
  return {
    etymology: '',
    confidence: '',
    fromLanguages: '',
    elements: [],
    references: '',
  }
}

function toEtymologyDraft(entry: Etymology): EtymologyDraft {
  return {
    etymology: entry.etymology_md,
    confidence: entry.confidence,
    fromLanguages: entry.from_languages.join(', '),
    elements: entry.elements.map(toElementDraft),
    references: entry.references.join('\n'),
  }
}

function toDraft(entry: NameEntry): NameDraft {
  return {
    name: entry.name,
    language: entry.language,
    isEndonym: entry.is_endonym,
    // A name saved with no etymology at all still needs one editable
    // block, or the section would render as an uneditable bare heading.
    etymologies:
      entry.etymologies.length > 0
        ? entry.etymologies.map(toEtymologyDraft)
        : [emptyEtymology()],
  }
}

function emptyDraft(name = ''): NameDraft {
  return {
    name,
    language: '',
    isEndonym: false,
    etymologies: [emptyEtymology()],
  }
}

function fromEtymologyDraft(draft: EtymologyDraft): Etymology {
  return {
    etymology_md: draft.etymology,
    confidence: draft.confidence,
    from_languages: draft.fromLanguages
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
    elements: draft.elements
      .filter((element) => element.form.trim())
      .map((element) => ({
        form: element.form.trim(),
        language: element.language.trim(),
        gloss: element.gloss.trim(),
        role: element.role,
        script: element.script,
        transliteration: element.transliteration,
      })),
    references: draft.references
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean),
  }
}

function fromDraft(draft: NameDraft): NameEntry {
  return {
    name: draft.name.trim(),
    language: draft.language.trim(),
    is_endonym: draft.isEndonym,
    etymologies: draft.etymologies.map(fromEtymologyDraft),
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
  const [mdHelpOpen, setMdHelpOpen] = useState(false)
  const [legalDoc, setLegalDoc] = useState<LegalDoc | null>(null)

  const updateName = (index: number, patch: Partial<NameDraft>) => {
    setNames((prev) =>
      prev.map((draft, i) => (i === index ? { ...draft, ...patch } : draft)),
    )
  }

  const updateEtymology = (
    nameIndex: number,
    etymologyIndex: number,
    patch: Partial<EtymologyDraft>,
  ) => {
    setNames((prev) =>
      prev.map((draft, i) =>
        i === nameIndex
          ? {
              ...draft,
              etymologies: draft.etymologies.map((etymology, j) =>
                j === etymologyIndex ? { ...etymology, ...patch } : etymology,
              ),
            }
          : draft,
      ),
    )
  }

  const updateElement = (
    nameIndex: number,
    etymologyIndex: number,
    elementIndex: number,
    patch: Partial<ElementDraft>,
  ) => {
    setNames((prev) =>
      prev.map((draft, i) =>
        i === nameIndex
          ? {
              ...draft,
              etymologies: draft.etymologies.map((etymology, j) =>
                j === etymologyIndex
                  ? {
                      ...etymology,
                      elements: etymology.elements.map((element, k) =>
                        k === elementIndex
                          ? { ...element, ...patch }
                          : element,
                      ),
                    }
                  : etymology,
              ),
            }
          : draft,
      ),
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
          const check = (code: string): string => {
            const normal = normalizeCode(code, table)
            if (normal === null) {
              bad.add(code.trim())
              return code
            }
            return normal
          }
          checked = entries.map((entry) => ({
            ...entry,
            language: check(entry.language),
            etymologies: entry.etymologies.map((etymology) => ({
              ...etymology,
              from_languages: etymology.from_languages.map(check),
              elements: etymology.elements.map((element) => ({
                ...element,
                language: check(element.language),
              })),
            })),
          }))
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

          {draft.etymologies.map((etymology, etyIndex) => (
            <div className="etymology-editor" key={etyIndex}>
              {/* Only labelled once there is more than one to tell apart,
                  so the ordinary single-etymology form is unchanged. */}
              {draft.etymologies.length > 1 && (
                <div className="etymology-editor-head">
                  <span>
                    {etymologyHeading(etyIndex, draft.etymologies.length)}
                  </span>
                  <button
                    type="button"
                    className="etymology-editor-remove"
                    onClick={() =>
                      updateName(index, {
                        etymologies: draft.etymologies.filter(
                          (_, j) => j !== etyIndex,
                        ),
                      })
                    }
                  >
                    Remove
                  </button>
                </div>
              )}
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
                  value={etymology.fromLanguages}
                  onChange={(e) =>
                    updateEtymology(index, etyIndex, {
                      fromLanguages: e.target.value,
                    })
                  }
                  placeholder="oji, fra"
                />
              </label>
              <label>
                <span className="label-with-help">
                  Etymology (Markdown)
                  <button
                    type="button"
                    className="lang-help-button"
                    aria-label="Markdown formatting help"
                    onClick={(e) => {
                      // keep the label from focusing the textarea
                      e.preventDefault()
                      setMdHelpOpen(true)
                    }}
                  >
                    ?
                  </button>
                </span>
                <textarea
                  value={etymology.etymology}
                  onChange={(e) =>
                    updateEtymology(index, etyIndex, {
                      etymology: e.target.value,
                    })
                  }
                  rows={6}
                  placeholder={
                    index === 0 && etyIndex === 0
                      ? 'What does this name mean? Where does it come from?'
                      : undefined
                  }
                />
              </label>
              <label className="etymology-confidence-field">
                How well established?
                <select
                  value={etymology.confidence}
                  onChange={(e) =>
                    updateEtymology(index, etyIndex, {
                      confidence: e.target.value as Confidence,
                    })
                  }
                >
                  {CONFIDENCE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              {/* Collapsed by default: the prose box is the floor, and
                  nobody should have to fill a table to write an article. */}
              <details className="element-editor">
                <summary>
                  Word breakdown
                  {etymology.elements.length > 0 &&
                    ` (${etymology.elements.length})`}
                </summary>
                <p className="element-editor-hint">
                  The words the name is built from — e.g. <em>nemos</em>,
                  Gaulish, ‘sacred grove’. Optional, but it’s what makes the
                  etymology searchable rather than just readable.
                </p>
                {etymology.elements.map((element, elIndex) => (
                  <div className="element-editor-row" key={elIndex}>
                    <label>
                      Word
                      <input
                        value={element.form}
                        onChange={(e) =>
                          updateElement(index, etyIndex, elIndex, {
                            form: e.target.value,
                          })
                        }
                      />
                    </label>
                    <label className="element-editor-lang">
                      Language
                      <input
                        value={element.language}
                        onChange={(e) =>
                          updateElement(index, etyIndex, elIndex, {
                            language: e.target.value,
                          })
                        }
                        placeholder="lat"
                      />
                    </label>
                    <label>
                      Meaning
                      <input
                        value={element.gloss}
                        onChange={(e) =>
                          updateElement(index, etyIndex, elIndex, {
                            gloss: e.target.value,
                          })
                        }
                        placeholder="water"
                      />
                    </label>
                    <label className="element-editor-role">
                      Role
                      <select
                        value={element.role}
                        onChange={(e) =>
                          updateElement(index, etyIndex, elIndex, {
                            role: e.target.value as ElementRole,
                          })
                        }
                      >
                        {ROLE_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <button
                      type="button"
                      className="element-editor-remove"
                      aria-label="Remove word"
                      onClick={() =>
                        updateEtymology(index, etyIndex, {
                          elements: etymology.elements.filter(
                            (_, k) => k !== elIndex,
                          ),
                        })
                      }
                    >
                      ×
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="element-editor-add"
                  onClick={() =>
                    updateEtymology(index, etyIndex, {
                      elements: [...etymology.elements, emptyElement()],
                    })
                  }
                >
                  + Add word
                </button>
              </details>
              <label>
                References (one per line)
                <textarea
                  value={etymology.references}
                  onChange={(e) =>
                    updateEtymology(index, etyIndex, {
                      references: e.target.value,
                    })
                  }
                  rows={2}
                />
              </label>
            </div>
          ))}
          <button
            type="button"
            className="etymology-editor-add"
            onClick={() =>
              updateName(index, {
                etymologies: [...draft.etymologies, emptyEtymology()],
              })
            }
          >
            + Add a competing etymology
          </button>

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
      {mdHelpOpen && (
        <MarkdownHelpDialog onClose={() => setMdHelpOpen(false)} />
      )}
      {/* As a dialog rather than a link to /terms: navigating away mid-edit
          would lose the draft. */}
      {legalDoc && (
        <DocumentDialog
          doc={legalDoc}
          onClose={() => setLegalDoc(null)}
          onOpenDoc={setLegalDoc}
        />
      )}
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
        wording.{' '}
        <button
          type="button"
          className="about-terms-link"
          onClick={() => setLegalDoc('terms')}
        >
          See the Terms of Use
        </button>
        .
      </p>
    </form>
  )
}

export default ArticleEditor
