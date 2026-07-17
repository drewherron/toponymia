interface AboutDialogProps {
  onClose: () => void
}

/** Static about/licensing panel (DESIGN.md §6 licensing). Content is
 * CC BY-SA 4.0; software is AGPL-3.0; upstream data keeps its own terms. */
function AboutDialog({ onClose }: AboutDialogProps) {
  return (
    <div className="about-backdrop" onClick={onClose} role="presentation">
      <div
        className="about-dialog"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="About Toponymia"
      >
        <div className="about-header">
          <h2>About Toponymia</h2>
          <button
            type="button"
            className="about-close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <p>
          Toponymia is a map-based wiki about the origins and meanings of
          place names. The map is the index: you find articles by looking at
          the world, and any named feature — a town, river, road, or
          mountain — opens the article about how it got its name, or a stub
          inviting you to write it.
        </p>

        <h3>Licensing</h3>
        <p>
          Toponymia licenses its content and its software separately.
        </p>
        <ul>
          <li>
            <strong>Wiki content</strong> — articles, etymology, and talk
            posts — is licensed{' '}
            <a
              href="https://creativecommons.org/licenses/by-sa/4.0/"
              target="_blank"
              rel="noreferrer"
            >
              CC BY-SA 4.0
            </a>
            . You may reuse it with attribution, provided derivatives stay
            under the same license.
          </li>
          <li>
            <strong>Software</strong> is licensed under the{' '}
            <a
              href="https://www.gnu.org/licenses/agpl-3.0.html"
              target="_blank"
              rel="noreferrer"
            >
              GNU Affero GPL v3
            </a>
            .
          </li>
        </ul>

        <h3>Contributing</h3>
        <p>
          By saving an edit or posting to a talk page, you agree to release
          your contribution under CC BY-SA 4.0 and confirm you have the right
          to do so. You keep copyright in your work. Reusers credit
          “Toponymia contributors” with a link to the article and its
          revision history, which is the authoritative list of authors.
        </p>

        <h3>Data &amp; attribution</h3>
        <ul>
          <li>
            Map data ©{' '}
            <a
              href="https://www.openstreetmap.org/copyright"
              target="_blank"
              rel="noreferrer"
            >
              OpenStreetMap
            </a>{' '}
            contributors, under the Open Database License (ODbL).
          </li>
          <li>
            Basemap tiles by{' '}
            <a href="https://openfreemap.org/" target="_blank" rel="noreferrer">
              OpenFreeMap
            </a>
            .
          </li>
          <li>
            Identifiers and multilingual labels from{' '}
            <a
              href="https://www.wikidata.org/"
              target="_blank"
              rel="noreferrer"
            >
              Wikidata
            </a>{' '}
            (CC0, public domain).
          </li>
        </ul>
      </div>
    </div>
  )
}

export default AboutDialog
