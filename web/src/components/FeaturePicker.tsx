import type { FeatureCandidate } from '../types'

interface FeaturePickerProps {
  x: number
  y: number
  candidates: FeatureCandidate[]
  onSelect: (candidate: FeatureCandidate) => void
}

function FeaturePicker({ x, y, candidates, onSelect }: FeaturePickerProps) {
  return (
    <div className="feature-picker" style={{ left: x, top: y }}>
      {candidates.map((candidate) => (
        <button
          type="button"
          key={`${candidate.name}|${candidate.kind}`}
          className="feature-picker-item"
          onClick={() => onSelect(candidate)}
        >
          <span className="feature-name">{candidate.name}</span>
          <span className="feature-kind">{candidate.kind}</span>
        </button>
      ))}
    </div>
  )
}

export default FeaturePicker
