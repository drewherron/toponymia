/** Wording shared between reading an article and editing one. A reader
 *  who sees "Alternative etymology 2" should find that same heading when
 *  they open the form, so the labels live here rather than in either
 *  component. */

/** Heading for one hypothesis among several.
 *
 *  Alternatives are numbered among *themselves* — the second etymology is
 *  alternative 1 — and a lone alternative gets no number at all, since a
 *  "1" with nothing after it reads as a missing sibling rather than as a
 *  count. */
export function etymologyHeading(index: number, total: number): string {
  if (index === 0) return 'Etymology'
  if (total > 2) return `Alternative etymology ${index}`
  return 'Alternative etymology'
}
