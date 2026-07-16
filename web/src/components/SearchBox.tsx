import { useEffect, useRef, useState } from 'react'
import { searchArticles, searchGeocoder } from '../api'
import type { GeocodeHit, SearchResult } from '../types'

interface SearchBoxProps {
  onSelectArticle: (place: SearchResult) => void
  onSelectGeocode: (hit: GeocodeHit) => void
  /** Current map center, to bias geocoder results toward the view. */
  getCenter: () => { lng: number; lat: number } | null
}

const MIN_QUERY = 2
const DEBOUNCE_MS = 250

function SearchBox({
  onSelectArticle,
  onSelectGeocode,
  getCenter,
}: SearchBoxProps) {
  const [query, setQuery] = useState('')
  const [articles, setArticles] = useState<SearchResult[]>([])
  const [geocoded, setGeocoded] = useState<GeocodeHit[]>([])
  const [open, setOpen] = useState(false)
  const [searched, setSearched] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const trimmed = query.trim()
    if (trimmed.length < MIN_QUERY) {
      setArticles([])
      setGeocoded([])
      setSearched(false)
      return
    }
    const controller = new AbortController()
    const timer = setTimeout(() => {
      // The two halves of search (DESIGN.md §2.3): our articles and the
      // world at large. Either may fail without blanking the other.
      Promise.allSettled([
        searchArticles(trimmed, controller.signal),
        searchGeocoder(trimmed, getCenter(), controller.signal),
      ]).then(([own, geo]) => {
        if (controller.signal.aborted) return
        const found = own.status === 'fulfilled' ? own.value : []
        const refs = new Set(
          found
            .filter((r) => r.osm_type && r.osm_id)
            .map((r) => `${r.osm_type}/${r.osm_id}`),
        )
        setArticles(found)
        setGeocoded(
          geo.status === 'fulfilled'
            ? geo.value.filter((h) => !h.osmRef || !refs.has(h.osmRef))
            : [],
        )
        setSearched(true)
        setOpen(true)
      })
    }, DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [query, getCenter])

  const reset = () => {
    setQuery('')
    setOpen(false)
  }

  const pickArticle = (place: SearchResult) => {
    reset()
    onSelectArticle(place)
  }

  const pickGeocode = (hit: GeocodeHit) => {
    reset()
    onSelectGeocode(hit)
  }

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape') {
      reset()
      inputRef.current?.blur()
    } else if (event.key === 'Enter') {
      if (articles.length > 0) pickArticle(articles[0])
      else if (geocoded.length > 0) pickGeocode(geocoded[0])
    }
  }

  const hasResults = articles.length > 0 || geocoded.length > 0

  return (
    <div className="search-box">
      <input
        ref={inputRef}
        className="search-input"
        type="search"
        placeholder="Search places…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={handleKeyDown}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        aria-label="Search places"
      />
      {open && searched && (
        <div className="search-results">
          {articles.length > 0 && (
            <div className="search-group">Articles</div>
          )}
          {articles.map((place) => (
            <button
              key={place.slug}
              type="button"
              className="search-result search-result-article"
              // mousedown, not click: the input's blur closes the list
              // before a click would land
              onMouseDown={(e) => {
                e.preventDefault()
                pickArticle(place)
              }}
            >
              <span className="search-result-name">
                {place.display_name}
                {place.matched_name && (
                  <span className="search-result-alias">
                    {place.matched_name}
                  </span>
                )}
              </span>
              <span className="search-result-kind">
                {place.feature_class}
              </span>
            </button>
          ))}
          {geocoded.length > 0 && (
            <div className="search-group">Elsewhere on the map</div>
          )}
          {geocoded.map((hit) => (
            <button
              key={`${hit.osmRef ?? hit.name}|${hit.context}`}
              type="button"
              className="search-result"
              onMouseDown={(e) => {
                e.preventDefault()
                pickGeocode(hit)
              }}
            >
              <span className="search-result-name">
                {hit.name}
                {hit.context && (
                  <span className="search-result-context">
                    {hit.context}
                  </span>
                )}
              </span>
              <span className="search-result-kind">{hit.kind}</span>
            </button>
          ))}
          {!hasResults && (
            <div className="search-empty">No places found</div>
          )}
        </div>
      )}
    </div>
  )
}

export default SearchBox
