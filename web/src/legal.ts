// The site's legal documents: the repo's own Markdown, inlined at build time
// so the text a reader is shown is exactly the version in that deploy's git
// history — which is the versioning story both documents promise, and what
// core.terms.TERMS_VERSION records against each signup.
import termsMarkdown from '../../TERMS.md?raw'
import privacyMarkdown from '../../PRIVACY.md?raw'

export type LegalDoc = 'terms' | 'privacy'

export const DOC_PATHS: Record<LegalDoc, string> = {
  terms: '/terms',
  privacy: '/privacy',
}

export const DOC_TITLES: Record<LegalDoc, string> = {
  terms: 'Terms of Use',
  privacy: 'Privacy Policy',
}

// The dialog header carries the title, so drop each document's own H1 rather
// than showing it twice.
export const DOC_BODIES: Record<LegalDoc, string> = {
  terms: termsMarkdown.replace(/^#[^\n]*\n/, ''),
  privacy: privacyMarkdown.replace(/^#[^\n]*\n/, ''),
}

const PATH_TO_DOC = new Map<string, LegalDoc>(
  (Object.keys(DOC_PATHS) as LegalDoc[]).map((doc) => [DOC_PATHS[doc], doc]),
)

/** The document a path names, or null. Used both to open the right dialog on
 *  a deep link and to keep the documents' cross-links in-app. */
export function docForPath(pathname: string): LegalDoc | null {
  return PATH_TO_DOC.get(pathname.replace(/\/$/, '')) ?? null
}
