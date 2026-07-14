import { useCallback, useEffect, useState } from 'react'
import { fetchMe } from './api'
import AuthControl from './components/AuthControl'
import FeaturePane from './components/FeaturePane'
import FeaturePicker from './components/FeaturePicker'
import MapView from './map/MapView'
import type { ClickContext, FeatureCandidate, User } from './types'

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
  const [user, setUser] = useState<User | null>(null)
  const [authOpen, setAuthOpen] = useState(false)
  const [allArticles, setAllArticles] = useState(false)
  const [highlightsEpoch, setHighlightsEpoch] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    // also plants the CSRF cookie needed for resolve/login/save
    fetchMe(controller.signal)
      .then(setUser)
      .catch((error: unknown) => {
        if (!controller.signal.aborted) console.error(error)
      })
    return () => controller.abort()
  }, [])

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

  const handleArticleSaved = useCallback(
    () => setHighlightsEpoch((epoch) => epoch + 1),
    [],
  )

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
        allArticles={allArticles}
        highlightsEpoch={highlightsEpoch}
      />
      <AuthControl
        user={user}
        onUserChange={setUser}
        open={authOpen}
        onOpenChange={setAuthOpen}
      />
      <button
        type="button"
        className={`articles-toggle${allArticles ? ' active' : ''}`}
        onClick={() => setAllArticles((value) => !value)}
        aria-pressed={allArticles}
      >
        All articles
      </button>
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
          user={user}
          onRequestAuth={() => setAuthOpen(true)}
          onClose={() => setSelected(null)}
          onArticleSaved={handleArticleSaved}
        />
      )}
    </div>
  )
}

export default App
