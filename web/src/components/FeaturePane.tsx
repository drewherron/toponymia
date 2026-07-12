import type { FeatureCandidate } from '../types'

interface FeaturePaneProps {
  feature: FeatureCandidate
  onClose: () => void
}

function FeaturePane({ feature, onClose }: FeaturePaneProps) {
  return (
    <aside className="feature-pane">
      <div className="feature-pane-header">
        <div>
          <span className="feature-kind">{feature.kind}</span>
          <h1>{feature.name}</h1>
        </div>
        <button
          type="button"
          className="feature-pane-close"
          onClick={onClose}
          aria-label="Close"
        >
          ×
        </button>
      </div>
      <p className="feature-pane-note">
        No article yet. Article view coming in a later milestone — below is the
        raw map feature.
      </p>
      <table className="feature-props">
        <tbody>
          {Object.entries(feature.properties).map(([key, value]) => (
            <tr key={key}>
              <th>{key}</th>
              <td>{String(value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </aside>
  )
}

export default FeaturePane
