interface MarkdownHelpDialogProps {
  onClose: () => void
}

// syntax → what it produces. Only what the renderer actually supports:
// react-markdown + remark-gfm, no raw HTML. Kept to the everyday basics —
// the point is a quick reminder, not the full CommonMark spec.
const rows: { syntax: string; result: string }[] = [
  { syntax: '*italic*', result: 'italic' },
  { syntax: '**bold**', result: 'bold' },
  { syntax: '[text](https://example.com)', result: 'a link' },
  { syntax: '[Paris](/place/paris)', result: 'a link to another place' },
  { syntax: '- item', result: 'a bulleted list (one per line)' },
  { syntax: '1. item', result: 'a numbered list' },
  { syntax: '> quote', result: 'a blockquote' },
  { syntax: '`code`', result: 'inline code' },
  { syntax: '~~struck~~', result: 'struck-through text' },
  { syntax: '## Heading', result: 'a heading' },
]

/** Centered overlay (same chrome as LanguageHelpDialog) with a quick reference
 * for the Markdown accepted in an etymology. */
function MarkdownHelpDialog({ onClose }: MarkdownHelpDialogProps) {
  return (
    <div className="about-backdrop" onClick={onClose} role="presentation">
      <div
        className="about-dialog markdown-help-dialog"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Markdown formatting"
      >
        <div className="about-header">
          <h2>Markdown formatting</h2>
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
          Etymology article text is written in Markdown. A blank line starts a new
          paragraph; everything else is optional shorthand:
        </p>

        <table className="markdown-help-table">
          <thead>
            <tr>
              <th>Type this</th>
              <th>To get</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.syntax}>
                <td>
                  <code>{row.syntax}</code>
                </td>
                <td>{row.result}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <p className="markdown-help-note">
          To link another place on the map, use its path:{' '}
          <code>[Paris](/place/paris)</code> — those open in the pane instead of
          reloading. Raw HTML is not rendered.
        </p>
      </div>
    </div>
  )
}

export default MarkdownHelpDialog
