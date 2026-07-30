import { useMemo } from 'react'
import Markdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { DOC_BODIES, DOC_TITLES, docForPath } from '../legal'
import type { LegalDoc } from '../legal'

interface DocumentDialogProps {
  doc: LegalDoc
  onClose: () => void
  /** Follow a cross-link to the other document, in place. */
  onOpenDoc: (doc: LegalDoc) => void
}

const plugins = [remarkGfm]

/** Terms of Use / Privacy Policy, rendered from the Markdown sources in the
 *  same chrome as AboutDialog. Also what /terms and /privacy render. */
function DocumentDialog({ doc, onClose, onOpenDoc }: DocumentDialogProps) {
  const components = useMemo<Components>(
    () => ({
      a({ href, children, ...rest }) {
        // The documents link to each other by path; keep those in the dialog
        // instead of reloading the whole app to show a sibling document.
        const sibling = href ? docForPath(href) : null
        if (sibling) {
          return (
            <a
              href={href}
              onClick={(event) => {
                if (event.metaKey || event.ctrlKey || event.shiftKey) return
                event.preventDefault()
                onOpenDoc(sibling)
              }}
              {...rest}
            >
              {children}
            </a>
          )
        }
        const external = href?.startsWith('http')
        return (
          <a
            href={href}
            {...(external ? { target: '_blank', rel: 'noreferrer' } : {})}
            {...rest}
          >
            {children}
          </a>
        )
      },
    }),
    [onOpenDoc],
  )

  return (
    <div
      className="about-backdrop terms-backdrop"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="about-dialog terms-dialog"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label={DOC_TITLES[doc]}
      >
        <div className="about-header">
          <h2>{DOC_TITLES[doc]}</h2>
          <button
            type="button"
            className="about-close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <Markdown remarkPlugins={plugins} components={components}>
          {DOC_BODIES[doc]}
        </Markdown>
      </div>
    </div>
  )
}

export default DocumentDialog
