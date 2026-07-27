import { useEffect, useMemo, useState } from 'react'
import { loadLanguages } from '../languages'
import type { LanguageEntry } from '../languages'

interface LanguageHelpDialogProps {
  onClose: () => void
}

const MAX_ROWS = 200

/** Centered overlay (same chrome as AboutDialog) explaining how to pick
 * "derived from" languages — the formation-vs-transfer rule — plus a
 * searchable table of every ISO 639-3 code. */
function LanguageHelpDialog({ onClose }: LanguageHelpDialogProps) {
  const [query, setQuery] = useState('')
  const [list, setList] = useState<LanguageEntry[] | null>(null)

  useEffect(() => {
    let cancelled = false
    loadLanguages().then((table) => {
      if (!cancelled) setList(table.list)
    })
    return () => {
      cancelled = true
    }
  }, [])

  const matches = useMemo(() => {
    if (!list) return []
    const q = query.trim().toLowerCase()
    if (!q) return list
    return list.filter(
      (entry) =>
        entry.code.startsWith(q) || entry.name.toLowerCase().includes(q),
    )
  }, [list, query])

  return (
    <div className="about-backdrop" onClick={onClose} role="presentation">
      <div
        className="about-dialog lang-help-dialog"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Language codes"
      >
        <div className="about-header">
          <h2>Language codes</h2>
          <button
            type="button"
            className="about-close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <p>
          Languages are written as{' '}
          <a
            href="https://en.wikipedia.org/wiki/ISO_639-3"
            target="_blank"
            rel="noreferrer"
          >
            ISO 639-3
          </a>{' '}
          codes: three letters, like <code>eng</code> (English),{' '}
          <code>fra</code> (French), or <code>lat</code> (Latin). Familiar
          two-letter codes (<code>en</code>, <code>fr</code>) are accepted
          and converted when you save.
        </p>

        <h3>Choosing “derived from” languages</h3>
        <p>
          Record the language(s) the <em>name</em> was formed in — not the
          deeper history of the words inside it.
        </p>
        <ul>
          <li>
            <strong>Follow a name transferred whole.</strong> Bethlehem,
            Pennsylvania carries the biblical name, formed in Hebrew →{' '}
            <code>hbo</code>. Mississippi borrows Ojibwe{' '}
            <em>misi-ziibi</em> whole → <code>oji</code>.
          </li>
          <li>
            <strong>Don’t follow component words.</strong> Portland was
            formed in English from <em>port</em> + <em>land</em>; that{' '}
            <em>port</em> descends from Latin <em>portus</em> is the history
            of the word, not the name → <code>eng</code>, not{' '}
            <code>lat</code>.
          </li>
          <li>
            <strong>Names inside names are components too.</strong> Johnson
            City was formed in English from the surname Johnson — stop
            there. The surname’s own trail (back to Hebrew{' '}
            <em>Yôḥānān</em>) belongs in the etymology text.
          </li>
          <li>
            <strong>Use the shallowest attested language.</strong>{' '}
            <code>ang</code> (Old English) if that’s when the name was
            formed — but never reconstructed proto-languages. ISO 639-3 has
            no codes for them; discuss them in the etymology text instead.
          </li>
        </ul>

        <h3>All codes</h3>
        <input
          className="lang-help-search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search by code or language name…"
          aria-label="Search language codes"
        />
        {list === null ? (
          <p className="lang-help-status">Loading code list…</p>
        ) : matches.length === 0 ? (
          <p className="lang-help-status">No matching language.</p>
        ) : (
          <>
            {matches.length > MAX_ROWS && (
              <p className="lang-help-status">
                Showing {MAX_ROWS} of {matches.length} languages — type to
                narrow.
              </p>
            )}
            <table className="lang-help-table">
              <tbody>
                {matches.slice(0, MAX_ROWS).map((entry) => (
                  <tr key={entry.code}>
                    <td>
                      <code>{entry.code}</code>
                    </td>
                    <td>{entry.name}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  )
}

export default LanguageHelpDialog
