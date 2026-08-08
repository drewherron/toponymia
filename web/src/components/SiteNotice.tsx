import type { Notice } from '../notice'

interface SiteNoticeProps {
  notice: Notice
  onDismiss: () => void
}

/** Map chrome, not a modal: the notice explains why the map looks the way it
 *  does, so it has to be readable *while* you look at the map. A backdrop
 *  dialog would be dismissed before the thing it describes had been seen.
 *  App hides it while the pane or the contributions lens is up — both cover
 *  this corner of the screen, and both mean the reader is already oriented.
 *
 *  Deliberately carries no sign-up button. The invitation to contribute is
 *  the last line of the text, and asking for an account before someone has
 *  even looked at the map puts the request ahead of the reason for it. */
function SiteNotice({ notice, onDismiss }: SiteNoticeProps) {
  return (
    <div className="site-notice" role="status" aria-label={notice.title}>
      <div className="site-notice-header">
        <h2>{notice.title}</h2>
        <button
          type="button"
          className="site-notice-close"
          onClick={onDismiss}
          aria-label="Dismiss this notice"
        >
          ×
        </button>
      </div>
      {notice.body.map((paragraph) => (
        <p key={paragraph}>{paragraph}</p>
      ))}
    </div>
  )
}

export default SiteNotice
