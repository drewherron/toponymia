import { useCallback, useState } from 'react'
import FeaturePane from './components/FeaturePane'
import FeaturePicker from './components/FeaturePicker'
import MapView from './map/MapView'
import type { ClickContext, FeatureCandidate } from './types'

interface PickerState {
  x: number
  y: number
  candidates: FeatureCandidate[]
  click: ClickContext
}

interface Selection {
  feature: FeatureCandidate
  click: ClickContext
}

function App() {
  const [picker, setPicker] = useState<PickerState | null>(null)
  const [selected, setSelected] = useState<Selection | null>(null)

  const handleClickFeatures = useCallback(
    (
      candidates: FeatureCandidate[],
      point: { x: number; y: number },
      click: ClickContext,
    ) => {
      if (candidates.length === 0) {
        setPicker(null)
        setSelected(null)
      } else if (candidates.length === 1) {
        setPicker(null)
        setSelected({ feature: candidates[0], click })
      } else {
        setPicker({ x: point.x, y: point.y, candidates, click })
      }
    },
    [],
  )

  const handleMoveStart = useCallback(() => setPicker(null), [])

  const handlePick = useCallback(
    (candidate: FeatureCandidate) => {
      if (!picker) return
      setSelected({ feature: candidate, click: picker.click })
      setPicker(null)
    },
    [picker],
  )

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
        <FeaturePane
          feature={selected.feature}
          click={selected.click}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  )
}

export default App
