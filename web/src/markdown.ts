/** Loader for the Markdown renderer, kept out of the component file so both
 *  the lazy component and the prefetch can share one import() — and so that
 *  file stays components-only for fast refresh. */
export const loadMarkdownRenderer = () => import('./components/MarkdownRenderer')

/** Warm the markdown chunk before anyone asks for it. Called on idle once the
 *  map has settled: opening the pane is the one thing every visitor does, and
 *  resolving a place for the first time can sit on Overpass for seconds — so
 *  fetching here means the renderer is already cached by the time a pane wants
 *  it, instead of queueing behind (or worse, after) that request. */
export function prefetchMarkdown() {
  void loadMarkdownRenderer()
}
