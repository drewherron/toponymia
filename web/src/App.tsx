import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchMe, fetchRandomArticle, getPlace } from './api'
import AboutDialog from './components/AboutDialog'
import AuthControl from './components/AuthControl'
import FeaturePane from './components/FeaturePane'
import FeaturePicker from './components/FeaturePicker'
import ModerationDashboard from './components/ModerationDashboard'
import ModQueue from './components/ModQueue'
import SearchBox from './components/SearchBox'
import {
  LABEL_LANGUAGES,
  storedLabelLanguage,
  storeLabelLanguage,
} from './map/labels'
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
  const [moderationOpen, setModerationOpen] = useState(false)
  const [aboutOpen, setAboutOpen] = useState(false)
  const [allArticles, setAllArticles] = useState(false)
  const [labelLanguage, setLabelLanguage] = useState(storedLabelLanguage)
  const [highlightsEpoch, setHighlightsEpoch] = useState(0)
  // Whether the selected place has scrolled out of the map viewport — drives
  // the pane's recenter button. The ref mirrors it so viewport callbacks (not
  // in React's render flow) can read the current place without re-subscribing.
  const [offView, setOffView] = useState(false)
  const selectedPlaceRef = useRef<ResolvedPlace | null>(null)
  // True while a fly-to the selected place's home view is in flight, so the
  // recenter button stays hidden until the camera lands (cleared on moveend).
  const pendingHomeRef = useRef(false)
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
    // A fly heads for the home view, so suppress the button until it lands;
    // opening without a fly (a hashed deep link) leaves it to comparison.
    pendingHomeRef.current = fly
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
        pendingHomeRef.current = false
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

  // The button shows once the camera has left the place's home view. While a
  // fly there is pending it stays hidden (we're on our way); no selection is
  // never "off view".
  const evalOffView = useCallback((place: ResolvedPlace | null) => {
    if (!place || pendingHomeRef.current) {
      setOffView(false)
      return
    }
    setOffView(mapApiRef.current?.isAtHomeView(place) === false)
  }, [])

  const handleResolved = useCallback(
    (place: ResolvedPlace) => {
      document.title = `${place.display_name} – Toponymia`
      const path = `/place/${place.slug}`
      if (window.location.pathname !== path) {
        window.history.pushState(null, '', path + window.location.hash)
      }
      selectedPlaceRef.current = place
      evalOffView(place)
    },
    [evalOffView],
  )

  const clearSelection = useCallback(() => {
    setSelected(null)
    selectedPlaceRef.current = null
    setOffView(false)
    document.title = 'Toponymia'
    if (window.location.pathname !== '/') {
      window.history.pushState(null, '', '/' + window.location.hash)
    }
  }, [])

  // In-article link to another place: swap the pane without moving the map
  // (the recenter button then offers the trip). No resolve — the pane fetches
  // the place by slug, exactly as a deep link or back/forward does.
  const handleSelectSlug = useCallback((slug: string) => {
    setPicker(null)
    pendingHomeRef.current = false // no fly — the button should offer the trip
    setSelected(slugSelection(slug, '…', '', { lng: 0, lat: 0 }))
  }, [])

  const handleRecenter = useCallback(() => {
    if (selectedPlaceRef.current) {
      pendingHomeRef.current = true
      setOffView(false)
      mapApiRef.current?.flyToPlace(selectedPlaceRef.current)
    }
  }, [])

  // Fires on every map settle: the fly (if any) has landed, so clear the
  // pending guard and re-test the live camera against the home view.
  const handleViewportChange = useCallback(() => {
    pendingHomeRef.current = false
    evalOffView(selectedPlaceRef.current)
  }, [evalOffView])

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
        pendingHomeRef.current = false
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
      pendingHomeRef.current = false
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
    pendingHomeRef.current = true // we're flying to the hit
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
        <button
          type="button"
          className={`articles-toggle${allArticles ? ' active' : ''}`}
          onClick={() => setAllArticles((value) => !value)}
          aria-pressed={allArticles}
        >
          All articles
        </button>
        <select
          className="lang-select"
          aria-label="Map label language"
          title="Map label language"
          value={labelLanguage}
          onChange={(event) => {
            storeLabelLanguage(event.target.value)
            setLabelLanguage(event.target.value)
          }}
        >
          {LABEL_LANGUAGES.map(({ code, label }) => (
            <option key={code} value={code}>
              {label}
            </option>
          ))}
        </select>
        {user?.is_moderator && (
          <button
            type="button"
            className="mod-queue-button"
            onClick={() => setModOpen(true)}
          >
            Reports
          </button>
        )}
        {user?.is_moderator && (
          <button
            type="button"
            className={`moderation-button${moderationOpen ? ' active' : ''}`}
            onClick={() => setModerationOpen((open) => !open)}
          >
            {moderationOpen ? 'Return to map' : 'Moderation'}
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
          onViewportChange={handleViewportChange}
          allArticles={allArticles}
          labelLanguage={labelLanguage}
          highlightsEpoch={highlightsEpoch}
          mapApi={mapApiRef}
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
            user={user}
            offView={offView}
            onRequestAuth={() => setAuthOpen(true)}
            onClose={clearSelection}
            onRecenter={handleRecenter}
            onSelectSlug={handleSelectSlug}
            onArticleSaved={handleArticleSaved}
            onResolved={handleResolved}
          />
        )}
        {moderationOpen && user?.is_moderator && <ModerationDashboard />}
      </div>
    </div>
  )
}

export default App
