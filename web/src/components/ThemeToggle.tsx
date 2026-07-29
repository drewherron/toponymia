import type { Theme } from '../theme'

interface Props {
  theme: Theme
  onToggle: () => void
}

/** The icon shows where the click goes, not where you are: a moon while
 *  the site is light (click for dark), a sun while it's dark. */
export default function ThemeToggle({ theme, onToggle }: Props) {
  const goingDark = theme === 'light'
  const label = goingDark ? 'Switch to dark mode' : 'Switch to light mode'

  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={onToggle}
      aria-label={label}
      title={label}
      aria-pressed={theme === 'dark'}
    >
      {goingDark ? <MoonIcon /> : <SunIcon />}
    </button>
  )
}

function MoonIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M13.6 9.8A5.9 5.9 0 0 1 6.2 2.4a5.9 5.9 0 1 0 7.4 7.4Z" />
    </svg>
  )
}

function SunIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="8" cy="8" r="3.1" />
      <path d="M8 1.2v1.5M8 13.3v1.5M14.8 8h-1.5M2.7 8H1.2M12.8 3.2l-1.05 1.05M4.25 11.75 3.2 12.8M12.8 12.8l-1.05-1.05M4.25 4.25 3.2 3.2" />
    </svg>
  )
}
