import { useCallback, useState } from 'react'
import FeaturePane from './components/FeaturePane'
import FeaturePicker from './components/FeaturePicker'
import MapView from './map/MapView'
import type { FeatureCandidate } from './types'

interface PickerState {
  x: number
  y: number
  candidates: FeatureCandidate[]
}

function App() {
  const [picker, setPicker] = useState<PickerState | null>(null)
  const [selected, setSelected] = useState<FeatureCandidate | null>(null)

  const handleClickFeatures = useCallback(
    (candidates: FeatureCandidate[], point: { x: number; y: number }) => {
      if (candidates.length === 0) {
        setPicker(null)
        setSelected(null)
      } else if (candidates.length === 1) {
        setPicker(null)
        setSelected(candidates[0])
      } else {
        setPicker({ x: point.x, y: point.y, candidates })
      }
    },
    [],
  )

  const handleMoveStart = useCallback(() => setPicker(null), [])

  const handlePick = useCallback((candidate: FeatureCandidate) => {
    setPicker(null)
    setSelected(candidate)
  }, [])

  return (
    <div className="app-shell">
      <MapView
        onClickFeatures={handleClickFeatures}
        onMoveStart={handleMoveStart}
      />
      {picker && (
        <FeaturePicker
          x={picker.x}
          y={picker.y}
          candidates={picker.candidates}
          onSelect={handlePick}
        />
      )}
      {selected && (
        <FeaturePane feature={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  )
}

export default App
