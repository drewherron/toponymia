import { Suspense, useMemo } from 'react'
import type { Components } from 'react-markdown'
import { etymologyHeading } from '../etymology'
import { useLanguageNames } from '../languages'
import type { ArticleData, Confidence } from '../types'
import MarkdownBody from './MarkdownBody'

interface ArticleViewProps {
  article: ArticleData
  /** Follow an in-article link to another place (by slug) in-pane. When
   *  omitted (e.g. a historical revision), internal links are plain anchors
   *  that navigate normally. */
  onSelectSlug?: (slug: string) => void
}

/** Wording is deliberate: "folk etymology" is a label *about* a tradition,
 *  not an endorsement of it, and "not stated" would read as a gap in the
 *  article rather than as a fact about the scholarship. */
const CONFIDENCE_LABEL: Record<Exclude<Confidence, ''>, string> = {
  attested: 'attested',
  probable: 'probable',
  proposed: 'proposed',
  disputed: 'disputed',
  folk: 'folk etymology',
  unknown: 'origin unknown',
}

/* An element's role is collected in the editor and kept in the record,
   but not shown here: it's a classification for later use, and reads as
   clutter beside the word it labels. */

/** An ISO 639-3 code, spelled out on hover once the name table has
 *  loaded. Unknown codes (and the moment before the table arrives) render
 *  as the bare code, so nothing depends on the lazy import having
 *  finished. */
function LanguageCode({
  code,
  names,
  className,
}: {
  code: string
  names: Map<string, string> | null
  className?: string
}) {
  const name = names?.get(code)
  if (!name) return <span className={className}>{code}</span>
  return (
    <abbr className={className} title={name}>
      {code}
    </abbr>
  )
}

const SLUG_PATH = /^\/place\/([\w-]+)\/?$/

/** A same-origin `/place/<slug>` href → its slug, else null (external link,
 *  or a link to some other path). Relative and absolute-same-host both work;
 *  cross-origin links (Wikipedia, etc.) fall through to a normal anchor. */
function internalSlug(href: string): string | null {
  try {
    const url = new URL(href, window.location.origin)
    if (url.origin !== window.location.origin) return null
    return SLUG_PATH.exec(url.pathname)?.[1] ?? null
  } catch {
    return null
  }
}

// References are free text; turn any http(s) URL inside one into a link
// while leaving the surrounding citation text alone. Trailing punctuation
// (a closing paren or sentence period) is kept out of the link.
const URL_RE = /(https?:\/\/[^\s]+)/g

function linkify(text: string) {
  return text.split(URL_RE).map((part, i) => {
    if (i % 2 === 0) return part // non-URL segment
    const trailing = part.match(/[.,;:!?)\]]+$/)?.[0] ?? ''
    const url = trailing ? part.slice(0, -trailing.length) : part
    return (
      <span key={i}>
        <a href={url} target="_blank" rel="noreferrer">
          {url}
        </a>
        {trailing}
      </span>
    )
  })
}

function ArticleView({ article, onSelectSlug }: ArticleViewProps) {
  const { content } = article
  // Only articles that actually show a code pay for the language table.
  const hasCodes = useMemo(
    () =>
      content.names.some(
        (entry) =>
          entry.language ||
          entry.etymologies.some(
            (etymology) =>
              etymology.from_languages.length > 0 ||
              etymology.elements.some((element) => element.language),
          ),
      ),
    [content.names],
  )
  const languageNames = useLanguageNames(hasCodes)
  // Intercept in-article `/place/<slug>` links so they swap the pane instead
  // of triggering a full reload; external links keep the reference style.
  const components = useMemo<Components>(
    () => ({
      a({ href, children, ...rest }) {
        const slug = href ? internalSlug(href) : null
        if (slug && onSelectSlug) {
          const handler = onSelectSlug
          return (
            <a
              href={href}
              className="place-link"
              onClick={(event) => {
                // Let modified clicks (new tab/window) behave normally.
                if (
                  event.metaKey ||
                  event.ctrlKey ||
                  event.shiftKey ||
                  event.altKey ||
                  event.button !== 0
                ) {
                  return
                }
                event.preventDefault()
                handler(slug)
              }}
              {...rest}
            >
              {children}
            </a>
          )
        }
        // A place link with no in-pane handler navigates normally (same tab);
        // any other link is external and opens in a new tab.
        if (slug) {
          return (
            <a href={href} className="place-link" {...rest}>
              {children}
            </a>
          )
        }
        return (
          <a href={href} target="_blank" rel="noreferrer" {...rest}>
            {children}
          </a>
        )
      },
    }),
    [onSelectSlug],
  )
  const body = (
    <div className="article">
      {/* Free-form bodies aren't written anymore; render only legacy
          revisions that still carry one (history must stay honest). */}
      {content.body_md.trim() !== '' && (
        <div className="article-body">
          <MarkdownBody components={components}>{content.body_md}</MarkdownBody>
        </div>
      )}

      {content.names.length > 0 && (
        <section className="article-names">
          <h2>Names</h2>
          {content.names.map((entry) => (
            <div className="article-name" key={`${entry.name}|${entry.language}`}>
              {/* Its own class because an etymology's Markdown renders
                  `###` as a bare h3 in this same container — without it the
                  name and its own subsections style identically. */}
              <h3 className="article-name-heading">
                {entry.name}
                {entry.language && (
                  <LanguageCode
                    className="name-language"
                    code={entry.language}
                    names={languageNames}
                  />
                )}
                {entry.is_endonym && (
                  <span className="name-endonym">endonym</span>
                )}
              </h3>
              {entry.etymologies.map((etymology, index) => (
                <div className="name-etymology" key={index}>
                  {/* Numbered only when there's something to tell apart —
                      a single etymology reads as the article's answer, not
                      as "theory 1 of 1". */}
                  {entry.etymologies.length > 1 && (
                    <h4 className="etymology-heading">
                      {etymologyHeading(index, entry.etymologies.length)}
                    </h4>
                  )}
                  {etymology.confidence && (
                    <p
                      className={
                        'etymology-confidence ' +
                        `confidence-${etymology.confidence}`
                      }
                    >
                      {CONFIDENCE_LABEL[etymology.confidence]}
                    </p>
                  )}
                  {etymology.from_languages.length > 0 && (
                    <p className="name-from">
                      from{' '}
                      {etymology.from_languages.map((code, i) => (
                        <span key={code}>
                          {i > 0 && ', '}
                          <LanguageCode code={code} names={languageNames} />
                        </span>
                      ))}
                    </p>
                  )}
                  {etymology.elements.length > 0 && (
                    <table className="etymology-elements">
                      <tbody>
                        {etymology.elements.map((element, i) => (
                          <tr key={i}>
                            <th scope="row">
                              {element.form}
                              {element.transliteration && (
                                <span className="element-translit">
                                  {element.transliteration}
                                </span>
                              )}
                            </th>
                            <td className="element-language">
                              {element.language && (
                                <LanguageCode
                                  code={element.language}
                                  names={languageNames}
                                />
                              )}
                            </td>
                            <td className="element-gloss">
                              {element.gloss && `‘${element.gloss}’`}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  {etymology.etymology_md && (
                    <MarkdownBody components={components}>
                      {etymology.etymology_md}
                    </MarkdownBody>
                  )}
                  {etymology.references.length > 0 && (
                    <ul className="name-references">
                      {etymology.references.map((ref) => (
                        <li key={ref}>{linkify(ref)}</li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          ))}
        </section>
      )}

      {content.derivations.length > 0 && (
        <section className="article-derivations">
          <h2>Derived terms</h2>
          <ul>
            {content.derivations.map((d) => (
              <li key={d.term}>
                {d.url ? (
                  <a href={d.url} target="_blank" rel="noreferrer">
                    {d.term}
                  </a>
                ) : (
                  d.term
                )}
                {d.note && <> — {d.note}</>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {content.see_also.length > 0 && (
        <section className="article-see-also">
          <h2>See also</h2>
          <ul>
            {content.see_also.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      )}

      <p className="article-byline">
        Last edited by <strong>{article.author}</strong> on{' '}
        {new Date(article.created).toLocaleDateString()}
        {article.comment && <> — “{article.comment}”</>}
      </p>
    </div>
  )
  // One boundary for the whole article: the body and every etymology share the
  // same lazy renderer, so per-instance fallbacks would flash a row of notes
  // for a single fetch (usually already warm — see prefetchMarkdown).
  return (
    <Suspense fallback={<p className="feature-pane-note">Loading article…</p>}>
      {body}
    </Suspense>
  )
}

export default ArticleView
