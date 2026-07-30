import { useMemo } from 'react'
import Markdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
// The Terms are the repo's TERMS.md, inlined at build time — the document a
// user agreed to is then exactly the one in the deploy's git history, which is
// the attribution/versioning story TERMS.md §8 promises.
import termsMarkdown from '../../../TERMS.md?raw'

interface TermsDialogProps {
  onClose: () => void
}

const plugins = [remarkGfm]

// The dialog header already says "Terms of Use"; drop the document's own H1
// so the title isn't repeated.
const body = termsMarkdown.replace(/^#[^\n]*\n/, '')

const components: Components = {
  a({ href, children, ...rest }) {
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
}

/** The full Terms of Use, in the same chrome as AboutDialog. Reached from the
 *  About dialog, and the page that satisfies the DMCA requirement to publish
 *  the designated agent's contact somewhere publicly accessible. */
function TermsDialog({ onClose }: TermsDialogProps) {
  const content = useMemo(
    () => (
      <Markdown remarkPlugins={plugins} components={components}>
        {body}
      </Markdown>
    ),
    [],
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
        aria-label="Terms of Use"
      >
        <div className="about-header">
          <h2>Terms of Use</h2>
          <button
            type="button"
            className="about-close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {content}
      </div>
    </div>
  )
}

export default TermsDialog
