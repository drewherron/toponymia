import { useMemo } from 'react'
import Markdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ArticleData } from '../types'

interface ArticleViewProps {
  article: ArticleData
  /** Follow an in-article link to another place (by slug) in-pane. When
   *  omitted (e.g. a historical revision), internal links are plain anchors
   *  that navigate normally. */
  onSelectSlug?: (slug: string) => void
}

const plugins = [remarkGfm]

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
  return (
    <div className="article">
      {/* Free-form bodies aren't written anymore; render only legacy
          revisions that still carry one (history must stay honest). */}
      {content.body_md.trim() !== '' && (
        <div className="article-body">
          <Markdown remarkPlugins={plugins} components={components}>
            {content.body_md}
          </Markdown>
        </div>
      )}

      {content.names.length > 0 && (
        <section className="article-names">
          <h2>Names</h2>
          {content.names.map((entry) => (
            <div className="article-name" key={`${entry.name}|${entry.language}`}>
              <h3>
                {entry.name}
                {entry.language && (
                  <span className="name-language">{entry.language}</span>
                )}
                {entry.is_endonym && (
                  <span className="name-endonym">endonym</span>
                )}
              </h3>
              {entry.from_languages.length > 0 && (
                <p className="name-from">
                  from {entry.from_languages.join(', ')}
                </p>
              )}
              {entry.etymology_md && (
                <Markdown remarkPlugins={plugins} components={components}>
                  {entry.etymology_md}
                </Markdown>
              )}
              {entry.references.length > 0 && (
                <ul className="name-references">
                  {entry.references.map((ref) => (
                    <li key={ref}>{linkify(ref)}</li>
                  ))}
                </ul>
              )}
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
}

export default ArticleView
