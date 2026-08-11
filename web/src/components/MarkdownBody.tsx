import { lazy } from 'react'
import { loadMarkdownRenderer } from '../markdown'

/** Renders Markdown, loading the renderer on first use. Callers supply their
 *  own <Suspense> boundary — one per view rather than one per instance, so an
 *  article's body and its etymologies don't each flash a fallback. */
const MarkdownBody = lazy(loadMarkdownRenderer)

export default MarkdownBody
