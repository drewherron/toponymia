import { useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchContributions,
  fetchMe,
  fetchRandomArticle,
  getPlace,
} from './api'
import AboutDialog from './components/AboutDialog'
import AccountDialog from './components/AccountDialog'
import DocumentDialog from './components/DocumentDialog'
import { DOC_PATHS, DOC_TITLES, docForPath } from './legal'
import type { LegalDoc } from './legal'
import AuthControl from './components/AuthControl'
import FeaturePane from './components/FeaturePane'
import FeaturePicker from './components/FeaturePicker'
import { MapLanguageControl } from './components/MapLanguageControl'
import ModerationDashboard from './components/ModerationDashboard'
import ModQueue from './components/ModQueue'
import SearchBox from './components/SearchBox'
import ThemeToggle from './components/ThemeToggle'
import {
  HEADER_MENU_QUERY,
  NARROW_QUERY,
  useMediaQuery,
  type SheetDetent,
} from './layout'
import {
  LABEL_LANGUAGES,
  storedLabelLanguage,
  storeLabelLanguage,
} from './map/labels'
import MapView from './map/MapView'
import { dismissedNotice, dismissNotice, noticeFor } from './notice'
import SiteNotice from './components/SiteNotice'
import { applyTheme, storedTheme, storeTheme, type Theme } from './theme'
import type {
  ClickContext,
  Contributions,
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

function currentDoc(): LegalDoc | null {
  return docForPath(window.location.pathname)
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
  // Registration is closed during the pre-launch window (PRELAUNCH in
  // settings.py). Assume open until /api/me/ says otherwise, so a slow
  // probe never flashes a 'closed' notice at a site that is open.
  const [signupsOpen, setSignupsOpen] = useState(true)
  const [authOpen, setAuthOpen] = useState(false)
  const [modOpen, setModOpen] = useState(false)
  const [moderationOpen, setModerationOpen] = useState(false)
  const [aboutOpen, setAboutOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  // /terms and /privacy are real URLs (server/core/spa.py serves them 200)
  // rendered as a dialog, so they can be linked, shared and crawled — the DMCA
  // agent contact has to be publicly reachable, not just findable via About.
  const [openDoc, setOpenDoc] = useState<LegalDoc | null>(currentDoc)
  // Where closing the dialog should return to. A direct visit to a document
  // has nowhere to go back to, hence the map root as the default.
  const docReturnRef = useRef('/')
  const [allArticles, setAllArticles] = useState(false)
  // The contributions lens: the fetched footprint, or null when it's off —
  // one piece of state for "on" and "what to draw", so they can't disagree.
  // Mutually exclusive with allArticles: two sets of dots meaning different
  // things, with nothing on screen to tell them apart, is just noise.
  const [contributions, setContributions] = useState<Contributions | null>(null)
  // Which notice this browser has dismissed (see notice.ts). Read once at
  // mount; the card compares it against the *current* notice's id, so
  // shipping a new announcement re-shows one to everybody.
  const [dismissed, setDismissed] = useState(dismissedNotice)
  const [labelLanguage, setLabelLanguage] = useState(storedLabelLanguage)
  const [theme, setTheme] = useState<Theme>(storedTheme)
  const [highlightsEpoch, setHighlightsEpoch] = useState(0)
  // Two independent breakpoints: the header collapses first (900), the pane
  // becomes a sheet later (768).
  const narrow = useMediaQuery(NARROW_QUERY)
  const compactHeader = useMediaQuery(HEADER_MENU_QUERY)
  // Half: enough article to read, enough map to stay oriented — on this
  // product the map is the index, so a sheet that buries it removes the only
  // way to navigate.
  const [sheetDetent, setSheetDetent] = useState<SheetDetent>('half')
  const [menuOpen, setMenuOpen] = useState(false)
  // The map's label drop-up. Held here, not in the control, so that crossing
  // into the compact header — which swaps the control for the menu's select —
  // doesn't leave an open menu to spring back on the way out.
  const [langOpen, setLangOpen] = useState(false)
  // Whether the selected place has scrolled out of the map viewport — drives
  // the pane's recenter button. The ref mirrors it so viewport callbacks (not
  // in React's render flow) can read the current place without re-subscribing.
  const [offView, setOffView] = useState(false)
  const selectedPlaceRef = useRef<ResolvedPlace | null>(null)
  // True while a fly-to the selected place's home view is in flight, so the
  // recenter button stays hidden until the camera lands (cleared on moveend).
  const pendingHomeRef = useRef(false)
  // A slug set here asks the next selection change to show that place's focus
  // highlight instead of tearing one down — how "Random article" gets the same
  // course "zoom to place" draws, without arming it for deep links or search.
  const pendingFocusRef = useRef<string | null>(null)
  const mapApiRef = useRef<MapApi | null>(null)
  // Captured at first render: MapLibre (hash: true) writes its own
  // #zoom/lat/lng into the URL as soon as the map mounts, so by effect
  // time location.hash no longer says whether the *link* carried one.
  const bootHashRef = useRef(window.location.hash)

  // index.html has already set data-theme from storage before this bundle
  // parsed, so on first render this is a no-op; it exists to carry later
  // toggles through to the attribute the stylesheet keys off.
  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  // Both of these take the control off screen while its menu could be open:
  // the compact header swaps it for the menu's select, and an article pane
  // covers the corner it sits in. Neither should leave an open menu behind to
  // spring back when the control returns.
  useEffect(() => {
    if (compactHeader || selected) setLangOpen(false)
  }, [compactHeader, selected])

  useEffect(() => {
    const controller = new AbortController()
    // also plants the CSRF cookie needed for resolve/login/save
    fetchMe(controller.signal)
      .then((me) => {
        setUser(me.user)
        setSignupsOpen(me.signupsOpen)
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) console.error(error)
      })
    return () => controller.abort()
  }, [])

  const openPlace = useCallback(
    (place: ResolvedPlace, fly: boolean, animate = true) => {
      const [lng, lat] = place.label_point ?? place.centroid
      setPicker(null)
      // Opening a place (search pick, random, deep link) drops back to the map.
      setModerationOpen(false)
      // A fly heads for the home view, so suppress the button until it lands;
      // opening without a fly (a hashed deep link) leaves it to comparison.
      pendingHomeRef.current = fly
      setSelected(
        slugSelection(place.slug, place.display_name, place.feature_class, {
          lng,
          lat,
        }),
      )
      if (fly) mapApiRef.current?.flyToPlace(place, animate)
    },
    [],
  )

  // Deep link: /place/<slug> opens the pane; the map only flies there
  // when the URL carries no #zoom/lat/lng of its own (a shared link
  // keeps both, restoring the exact view it was copied from). The boot
  // framing is a jump, not an animation: a fresh link should arrive
  // already framed, and a jump can't be dropped by load-time frame
  // pressure the way a long animation can.
  useEffect(() => {
    const slug = pathSlug()
    if (!slug) return
    const controller = new AbortController()
    const fly = bootHashRef.current.length < 2
    getPlace(slug, controller.signal)
      .then(({ place }) => {
        // A shared link may point at an alias slug; heal the URL to the
        // canonical in place (replace, not push) so it never lingers in the
        // back stack and a later Copy link uses one address per place.
        if (place.slug !== slug) {
          window.history.replaceState(
            null,
            '',
            `/place/${place.slug}${window.location.hash}`,
          )
        }
        openPlace(place, fly, false)
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          console.error(error)
          window.history.replaceState(null, '', '/')
        }
      })
    return () => controller.abort()
  }, [openPlace])

  const openLegalDoc = useCallback((doc: LegalDoc) => {
    setAboutOpen(false)
    // Only remember the return path on the way in — following a cross-link
    // from one document to the other should still come back to where the
    // reader started, not to its sibling.
    if (currentDoc() === null) {
      docReturnRef.current = window.location.pathname + window.location.hash
    }
    if (window.location.pathname !== DOC_PATHS[doc]) {
      window.history.pushState(null, '', DOC_PATHS[doc])
    }
    document.title = `${DOC_TITLES[doc]} – Toponymia`
    setOpenDoc(doc)
  }, [])

  const closeLegalDoc = useCallback(() => {
    setOpenDoc(null)
    if (currentDoc() !== null) {
      window.history.pushState(null, '', docReturnRef.current)
      // Restored by the pane when the return path is a place.
      document.title = 'Toponymia'
    }
  }, [])

  // Back/forward re-open or close the pane to match the URL.
  useEffect(() => {
    const onPopState = () => {
      setOpenDoc(currentDoc())
      const slug = pathSlug()
      if (slug) {
        setPicker(null)
        pendingHomeRef.current = false
        setSelected(
          slugSelection(slug, '…', '', { lng: 0, lat: 0 }),
        )
      } else {
        setSelected(null)
        const doc = currentDoc()
        document.title = doc
          ? `${DOC_TITLES[doc]} – Toponymia`
          : 'Toponymia'
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
    setSheetDetent('half') // the next place opens at the default detent
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
      // Here and on "Random article", but not on deep links or search hits:
      // the highlight answers "where does this reach?", which recentering and
      // a context-free random landing both raise but a deliberate search or a
      // shared link don't.
      mapApiRef.current?.showFocusGeometry(selectedPlaceRef.current.slug)
      mapApiRef.current?.flyToPlace(selectedPlaceRef.current)
    }
  }, [])

  // The highlight belongs to one place, so a selection change ends it: closing
  // the pane, following an in-article link, back/forward, a delete. The one
  // exception is a change that itself asks for a highlight — "Random article"
  // opens a new place and wants its course drawn (showFocusGeometry clears any
  // prior one first, so there's no leak).
  useEffect(() => {
    const focusSlug = pendingFocusRef.current
    pendingFocusRef.current = null
    if (focusSlug) mapApiRef.current?.showFocusGeometry(focusSlug)
    else mapApiRef.current?.clearFocusGeometry()
  }, [selected])

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
    setModerationOpen(false) // picking a place returns to the map

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
        if (!place) return
        // Draw the course on arrival, like "zoom to place": a random landing
        // has no context, so "how far does this reach?" is exactly the question
        // it raises. Line features light up; cities keep only the amber label.
        pendingFocusRef.current = place.slug
        openPlace(place, true)
      })
      .catch(console.error)
  }, [openPlace])

  // Fetched fresh on every activation rather than cached: an edit made in
  // this session should show up the next time you ask, and the response is
  // one small request.
  const handleShowContributions = useCallback(() => {
    fetchContributions()
      .then((data) => {
        setAccountOpen(false)
        setModerationOpen(false) // it's a map lens — show the map
        setAllArticles(false)
        setContributions(data)
        // Nothing to frame: the chip says so rather than flying the camera
        // to a bbox that doesn't exist.
        if (data.bbox) mapApiRef.current?.flyToBounds(data.bbox)
      })
      .catch(console.error)
  }, [])

  // Which card is on screen depends on whether signups are open, and the two
  // carry different ids, so dismissal has to be recorded against the one the
  // reader actually saw.
  const notice = noticeFor(signupsOpen)
  const handleDismissNotice = useCallback(() => {
    if (!notice) return
    dismissNotice(notice.id)
    setDismissed(notice.id)
  }, [notice])

  const getMapCenter = useCallback(
    () => mapApiRef.current?.getCenter() ?? null,
    [],
  )

  const authControl = (
    <AuthControl
      user={user}
      signupsOpen={signupsOpen}
      onUserChange={(next) => {
        setUser(next)
        // Logging out takes the lens down with it: it's a view of who you
        // are, and there's no one to be once the session ends.
        if (next === null) setContributions(null)
      }}
      open={authOpen}
      onOpenChange={(open) => {
        setAuthOpen(open)
        // Narrow: the form is an overlay over the menu it was opened from, so
        // dismissing it (by backdrop or by logging in) should leave the map,
        // not the menu it came through.
        if (!open) setMenuOpen(false)
      }}
      inMenu={compactHeader}
      onOpenTerms={() => openLegalDoc('terms')}
      onOpenAccount={() => setAccountOpen(true)}
    />
  )

  // The header's tools: inline on a wide screen, inside the ☰ menu when the
  // bar can't hold them. Same nodes either way — the menu is a container, not
  // a second copy of the controls.
  const headerTools = (
    <>
      <button type="button" className="random-button" onClick={handleRandom}>
        Random article
      </button>
      <button
        type="button"
        className={`articles-toggle${allArticles ? ' active' : ''}`}
        onClick={() => {
          setModerationOpen(false) // it's a map overlay — show the map
          setContributions(null) // the two lenses are exclusive
          setAllArticles((value) => !value)
        }}
        aria-pressed={allArticles}
      >
        All articles
      </button>
      {/* Wide screens get this as map chrome, overlaid bottom-left, where its
          scope reads off its position. The ☰ menu is the one place that
          framing isn't available, and a drop-up over a phone-sized map would
          be in the way — so narrow keeps it here, as a plain select. */}
      {compactHeader && (
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
      )}
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
      <ThemeToggle
        theme={theme}
        onToggle={() => {
          const next = theme === 'dark' ? 'light' : 'dark'
          storeTheme(next)
          setTheme(next)
        }}
      />
      <button
        type="button"
        className="about-button"
        onClick={() => setAboutOpen(true)}
      >
        About
      </button>
    </>
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
        {compactHeader ? (
          <>
            <button
              type="button"
              className="header-menu-button"
              aria-label="Menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((open) => !open)}
            >
              ☰
            </button>
            {menuOpen && (
              <>
                <div
                  className="header-menu-backdrop"
                  onClick={() => setMenuOpen(false)}
                />
                <div
                  className="header-menu"
                  onClick={(event) => {
                    const target = event.target as HTMLElement
                    // Picking a tool dismisses the menu. Two exceptions: the
                    // language <select> fires change rather than a button
                    // click, so it stays open for a look at the new labels;
                    // and the auth buttons must not unmount their own form
                    // out from under themselves.
                    if (
                      target.closest('button') &&
                      !target.closest('.auth-control')
                    ) {
                      setMenuOpen(false)
                    }
                  }}
                >
                  {headerTools}
                  {authControl}
                </div>
              </>
            )}
          </>
        ) : (
          <>
            {headerTools}
            {authControl}
          </>
        )}
      </header>
      {modOpen && <ModQueue onClose={() => setModOpen(false)} />}
      {aboutOpen && (
        <AboutDialog
          onClose={() => setAboutOpen(false)}
          onOpenDoc={openLegalDoc}
        />
      )}
      {accountOpen && user && (
        <AccountDialog
          user={user}
          onClose={() => setAccountOpen(false)}
          onUserChange={(next) => {
            setUser(next)
            if (next === null) {
              setAccountOpen(false)
              setContributions(null)
            }
          }}
          onOpenDoc={openLegalDoc}
          onShowContributions={handleShowContributions}
        />
      )}
      {openDoc && (
        <DocumentDialog
          doc={openDoc}
          onClose={closeLegalDoc}
          onOpenDoc={openLegalDoc}
        />
      )}
      <div className="map-area">
        <MapView
          onClickFeatures={handleClickFeatures}
          onMoveStart={handleMoveStart}
          onViewportChange={handleViewportChange}
          allArticles={allArticles}
          contributions={contributions?.features ?? null}
          labelLanguage={labelLanguage}
          highlightsEpoch={highlightsEpoch}
          narrow={narrow}
          paneOpen={!!selected}
          sheetDetent={sheetDetent}
          mapApi={mapApiRef}
        />
        {/* The lens is launched from a dialog that dismisses itself, so
            this is the only thing on screen saying why the map is filtered
            — and the only way back out. */}
        {contributions && (
          <div className="contrib-chip" role="status">
            <span>
              {contributions.features.length === 0
                ? "You haven't written or discussed anything yet"
                : contributions.truncated
                  ? `Your contributions (first ${contributions.features.length})`
                  : 'Your contributions'}
            </span>
            <button
              type="button"
              onClick={() => setContributions(null)}
              aria-label="Stop showing your contributions"
            >
              ×
            </button>
          </div>
        )}
        {/* Held back while anything else owns the map: an open pane or the
            contributions lens both cover this corner, and both mean the
            reader has already found their way around. It returns when they
            close, until the × puts it away for good. */}
        {notice && dismissed !== notice.id && !selected && !contributions
          && !moderationOpen && (
          <SiteNotice notice={notice} onDismiss={handleDismissNotice} />
        )}
        {!compactHeader && (
          <MapLanguageControl
            value={labelLanguage}
            open={langOpen}
            covered={!!selected}
            onOpenChange={setLangOpen}
            onChange={(code) => {
              storeLabelLanguage(code)
              setLabelLanguage(code)
            }}
          />
        )}
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
            narrow={narrow}
            sheetDetent={sheetDetent}
            onSheetDetentChange={setSheetDetent}
            onRequestAuth={() => {
              // Narrow: the auth control lives in the menu, so the CTA has to
              // open the menu that hosts it — otherwise it sets a flag on an
              // unmounted component and appears to do nothing.
              setMenuOpen(true)
              setAuthOpen(true)
            }}
            onClose={clearSelection}
            onRecenter={handleRecenter}
            onSelectSlug={handleSelectSlug}
            onArticleSaved={handleArticleSaved}
            onResolved={handleResolved}
          />
        )}
        {moderationOpen && user?.is_moderator && (
          <ModerationDashboard user={user} />
        )}
      </div>
    </div>
  )
}

export default App
