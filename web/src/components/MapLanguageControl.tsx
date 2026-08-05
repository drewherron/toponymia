import { useEffect } from 'react'
import { LABEL_LANGUAGES } from '../map/labels'

/**
 * The map's label language, as map chrome rather than header chrome.
 *
 * It lives over the map's bottom-left corner on purpose: in the header,
 * beside the account controls, a bare language name reads as a *site*
 * language switcher, and nothing about the site is translated — articles
 * are English whatever the labels say. Sitting on the map, its scope is
 * legible from position alone. Opening upward is what lets it sit down
 * there at all; there is no room below.
 */
export function MapLanguageControl({
  value,
  open,
  covered,
  onOpenChange,
  onChange,
}: {
  value: string
  open: boolean
  covered: boolean
  onOpenChange: (open: boolean) => void
  onChange: (code: string) => void
}) {
  const current = LABEL_LANGUAGES.find((lang) => lang.code === value)

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onOpenChange(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onOpenChange])

  return (
    <div className={`map-lang${covered ? ' covered' : ''}`}>
      <button
        type="button"
        className={`map-lang-button${open ? ' active' : ''}`}
        aria-haspopup="true"
        aria-expanded={open}
        aria-label="Map label language"
        title="Map label language"
        onClick={() => onOpenChange(!open)}
      >
        <span className="map-lang-globe" aria-hidden="true">
          ◍
        </span>
        {current?.label ?? 'English'}
      </button>
      {open && (
        <>
          <div
            className="map-lang-backdrop"
            onClick={() => onOpenChange(false)}
          />
          <div className="map-lang-menu" aria-label="Map label language">
            {LABEL_LANGUAGES.map(({ code, label }) => (
              <button
                key={code}
                type="button"
                className={`map-lang-option${code === value ? ' active' : ''}`}
                aria-current={code === value}
                onClick={() => {
                  onChange(code)
                  onOpenChange(false)
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
