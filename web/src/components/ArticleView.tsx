import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ArticleData } from '../types'

interface ArticleViewProps {
  article: ArticleData
}

const plugins = [remarkGfm]

function ArticleView({ article }: ArticleViewProps) {
  const { content } = article
  return (
    <div className="article">
      <div className="article-body">
        <Markdown remarkPlugins={plugins}>{content.body_md}</Markdown>
      </div>

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
                <Markdown remarkPlugins={plugins}>
                  {entry.etymology_md}
                </Markdown>
              )}
              {entry.references.length > 0 && (
                <ul className="name-references">
                  {entry.references.map((ref) => (
                    <li key={ref}>{ref}</li>
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
