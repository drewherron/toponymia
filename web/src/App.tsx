import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchMe, fetchRandomArticle, getPlace } from './api'
import AboutDialog from './components/AboutDialog'
import AuthControl from './components/AuthControl'
import FeaturePane from './components/FeaturePane'
import FeaturePicker from './components/FeaturePicker'
import ModQueue from './components/ModQueue'
import SearchBox from './components/SearchBox'
import MapView from './map/MapView'
import type {
  ClickContext,
  FeatureCandidate,
  GeocodeHit,
  MapApi,
  ResolvedPlace,
  User,
} from './types'

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

const SLUG_PATH = /^\/place\/([\w-]+)\/?$/

function pathSlug(): string | null {
  const match = SLUG_PATH.exec(window.location.pathname)
  return match ? match[1] : null
}

/** A selection that already knows its place: pane skips resolution. */
function slugSelection(
  slug: string,
  name: string,
  kind: string,
  lngLat: { lng: number; lat: number },
): Selection {
  return {
    feature: { name, kind, sourceLayer: 'direct', slug, properties: {} },
    click: { lngLat, zoom: 0 },
  }
}

function App() {
  const [picker, setPicker] = useState<PickerState | null>(null)
  const [selected, setSelected] = useState<Selection | null>(null)
  const [user, setUser] = useState<User | null>(null)
  const [authOpen, setAuthOpen] = useState(false)
  const [modOpen, setModOpen] = useState(false)
  const [aboutOpen, setAboutOpen] = useState(false)
  const [allArticles, setAllArticles] = useState(false)
  const [highlightsEpoch, setHighlightsEpoch] = useState(0)
  const mapApiRef = useRef<MapApi | null>(null)
  // Captured at first render: MapLibre (hash: true) writes its own
  // #zoom/lat/lng into the URL as soon as the map mounts, so by effect
  // time location.hash no longer says whether the *link* carried one.
  const bootHashRef = useRef(window.location.hash)

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

  const openPlace = useCallback((place: ResolvedPlace, fly: boolean) => {
    const [lng, lat] = place.label_point ?? place.centroid
    setPicker(null)
    setSelected(
      slugSelection(place.slug, place.display_name, place.feature_class, {
        lng,
        lat,
      }),
    )
    if (fly) mapApiRef.current?.flyToPlace(place)
  }, [])

  // Deep link: /place/<slug> opens the pane; the map only flies there
  // when the URL carries no #zoom/lat/lng of its own (a shared link
  // keeps both, restoring the exact view it was copied from).
  useEffect(() => {
    const slug = pathSlug()
    if (!slug) return
    const controller = new AbortController()
    const fly = bootHashRef.current.length < 2
    getPlace(slug, controller.signal)
      .then(({ place }) => openPlace(place, fly))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          console.error(error)
          window.history.replaceState(null, '', '/')
        }
      })
    return () => controller.abort()
  }, [openPlace])

  // Back/forward re-open or close the pane to match the URL.
  useEffect(() => {
    const onPopState = () => {
      const slug = pathSlug()
      if (slug) {
        setPicker(null)
        setSelected(
          slugSelection(slug, '…', '', { lng: 0, lat: 0 }),
        )
      } else {
        setSelected(null)
        document.title = 'Toponymia'
      }
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const handleResolved = useCallback((place: ResolvedPlace) => {
    document.title = `${place.display_name} – Toponymia`
    const path = `/place/${place.slug}`
    if (window.location.pathname !== path) {
      window.history.pushState(null, '', path + window.location.hash)
    }
  }, [])

  const clearSelection = useCallback(() => {
    setSelected(null)
    document.title = 'Toponymia'
    if (window.location.pathname !== '/') {
      window.history.pushState(null, '', '/' + window.location.hash)
    }
  }, [])

  const handleClickFeatures = useCallback(
    (
      candidates: FeatureCandidate[],
      point: { x: number; y: number },
      click: ClickContext,
    ) => {
      if (candidates.length === 0) {
        setPicker(null)
        clearSelection()
      } else if (candidates.length === 1) {
        setPicker(null)
        setSelected({ feature: candidates[0], click })
      } else {
        setPicker({ x: point.x, y: point.y, candidates, click })
      }
    },
    [clearSelection],
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

  const handleSelectArticle = useCallback(
    (place: ResolvedPlace) => openPlace(place, true),
    [openPlace],
  )

  const handleSelectGeocode = useCallback((hit: GeocodeHit) => {
    // No article here (yet): fly over and resolve it like a map click,
    // anchored at the geocoder's own coordinates.
    setPicker(null)
    setSelected({
      feature: {
        name: hit.name,
        kind: hit.kind,
        sourceLayer: 'geocoder',
        properties: {},
        anchor: hit.lngLat,
      },
      click: { lngLat: hit.lngLat, zoom: 14 },
    })
    mapApiRef.current?.flyToHit(hit)
  }, [])

  const handleRandom = useCallback(() => {
    fetchRandomArticle()
      .then((place) => {
        if (place) openPlace(place, true)
      })
      .catch(console.error)
  }, [openPlace])

  const getMapCenter = useCallback(
    () => mapApiRef.current?.getCenter() ?? null,
    [],
  )

  return (
    <div className="app-shell">
      <header className="app-header">
        <a className="app-logo" href="/">
          Toponymia
        </a>
        <SearchBox
          onSelectArticle={handleSelectArticle}
          onSelectGeocode={handleSelectGeocode}
          getCenter={getMapCenter}
        />
        <button
          type="button"
          className="random-button"
          onClick={handleRandom}
        >
          Random article
        </button>
        {user?.is_moderator && (
          <button
            type="button"
            className="mod-queue-button"
            onClick={() => setModOpen(true)}
          >
            Reports
          </button>
        )}
        <button
          type="button"
          className="about-button"
          onClick={() => setAboutOpen(true)}
        >
          About
        </button>
        <AuthControl
          user={user}
          onUserChange={setUser}
          open={authOpen}
          onOpenChange={setAuthOpen}
        />
      </header>
      {modOpen && <ModQueue onClose={() => setModOpen(false)} />}
      {aboutOpen && <AboutDialog onClose={() => setAboutOpen(false)} />}
      <div className="map-area">
        <MapView
          onClickFeatures={handleClickFeatures}
          onMoveStart={handleMoveStart}
          allArticles={allArticles}
          highlightsEpoch={highlightsEpoch}
          mapApi={mapApiRef}
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
            onClose={clearSelection}
            onArticleSaved={handleArticleSaved}
            onResolved={handleResolved}
          />
        )}
      </div>
    </div>
  )
}

export default App
