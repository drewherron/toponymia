/** The site-wide notice: one card over the map, shown until dismissed.
 *
 *  Everything about what it says lives in `NOTICE` below.
 *
 *  - **To change the wording** for a notice people are already dismissing,
 *    edit the text and give it a **new `id`**. Dismissal is recorded against
 *    the id, so a new one shows the notice again to everyone — including the
 *    people who dismissed the last one. Reusing the id would leave them with
 *    a stale dismissal of a notice they never saw.
 *  - **To switch it off**, set `NOTICE` to `null`. Nothing else has to change;
 *    the card stops rendering.
 *
 *  Dismissal is per-browser (localStorage, like the theme and the map's label
 *  language), so a private window or a second device sees it again. That's the
 *  accepted cost of not asking an anonymous visitor to have an account before
 *  the site can remember anything about them — and a reason to keep whatever
 *  goes here short and worth reading twice.
 */
export interface Notice {
  /** Bump this whenever the text changes. See the note above. */
  id: string
  title: string
  /** One string per paragraph. */
  body: string[]
}

export const NOTICE: Notice | null = {
  id: 'launch-2026',
  title: 'Welcome to Toponymia',
  body: [
    'This is a map-based wiki of place-name etymologies. Click any label on' +
      ' the map to read where that name came from, or to write the article' +
      ' yourself. Labels in amber already have articles. Click “All' +
      ' articles” to show where any existing articles are.',
    "This site is brand new, so most places don't have an article yet. An" +
      ' empty map is an invitation, not a fault. Anywhere you click without' +
      ' finding an article is a place-name nobody has explained yet, and you' +
      ' are welcome to be the one who does.',
  ],
}

/**
 * Shown instead while registration is closed (`PRELAUNCH` in settings.py).
 *
 * The card above invites the reader to write an article, which is the right
 * thing to say to everyone except someone who cannot make an account yet —
 * being told an empty map is an invitation, and then finding no way in, is
 * worse than an empty map on its own.
 *
 * **A separate id, not edited text.** Dismissal is recorded per id, so a
 * reader who dismisses this one still gets the real welcome when the site
 * opens — which is the point, because by then it says something new.
 */
export const PRELAUNCH_NOTICE: Notice | null = {
  id: 'prelaunch-2026',
  title: 'Toponymia is being written',
  body: [
    'This is a map-based wiki of place-name etymologies. Click any label on' +
      ' the map to read where that name came from. Labels in amber already' +
      ' have articles. Click “All articles” to show where any existing' +
      ' articles are.',
    'The wiki is still being seeded, so most places don’t have an article' +
      ' yet and new accounts aren’t open. Both change shortly — have a look' +
      ' around in the meantime.',
  ],
}

/** The notice for the site's current state.
 *
 * `null` means the site's state isn't known yet (the /api/me/ probe hasn't
 * answered), and nothing should be shown. The two cards have different ids,
 * so guessing and correcting is visible: a reader who dismissed one gets a
 * flash of the other on every load until the probe lands. Better a card that
 * arrives a moment late than one that appears and takes itself back.
 */
export function noticeFor(signupsOpen: boolean | null): Notice | null {
  if (signupsOpen === null) return null
  return signupsOpen ? NOTICE : PRELAUNCH_NOTICE
}

const STORAGE_KEY = 'toponymia:noticeDismissed'

/** The id of the last notice this browser dismissed, if any. */
export function dismissedNotice(): string | null {
  return localStorage.getItem(STORAGE_KEY)
}

export function dismissNotice(id: string) {
  localStorage.setItem(STORAGE_KEY, id)
}
