import Markdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

const plugins = [remarkGfm]

interface MarkdownRendererProps {
  children: string
  /** Element overrides (link interception, mostly); same shape react-markdown
   *  takes, since that's all this forwards to. */
  components?: Components
}

/** The only module that pulls in react-markdown and the remark/micromark
 *  stack — about 110 kB raw that nothing on first paint needs. Everything
 *  else imports MarkdownBody, which loads this on demand. */
function MarkdownRenderer({ children, components }: MarkdownRendererProps) {
  return (
    <Markdown remarkPlugins={plugins} components={components}>
      {children}
    </Markdown>
  )
}

export default MarkdownRenderer
