import { useState } from 'react'
import type { FormEvent } from 'react'
import { fetchMe, login, logout, signup } from '../api'
import type { User } from '../types'

interface AuthControlProps {
  user: User | null
  onUserChange: (user: User | null) => void
  open: boolean
  onOpenChange: (open: boolean) => void
  /** In the ☰ menu rather than the header bar: the form can't be a dropdown
   *  hanging off a dropdown, so it opens as a centred overlay instead. */
  narrow: boolean
}

function AuthControl({
  user,
  onUserChange,
  open,
  onOpenChange,
  narrow,
}: AuthControlProps) {
  const [mode, setMode] = useState<'login' | 'signup'>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const handleLogout = () => {
    logout()
      .then(() => onUserChange(null))
      .catch(console.error)
  }

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const action = mode === 'login' ? login : signup
    action(username, password)
      .then(() => fetchMe())
      .then((me) => {
        onUserChange(me)
        onOpenChange(false)
        setUsername('')
        setPassword('')
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setBusy(false))
  }

  if (user) {
    return (
      <div className="auth-control">
        <span className="auth-username">{user.username}</span>
        <button type="button" className="auth-link" onClick={handleLogout}>
          Log out
        </button>
      </div>
    )
  }

  const form = (
    <form
      className={`auth-form${narrow ? ' auth-overlay' : ''}`}
      onSubmit={handleSubmit}
    >
      <div className="auth-tabs">
        <button
          type="button"
          className={mode === 'login' ? 'active' : ''}
          onClick={() => setMode('login')}
        >
          Log in
        </button>
        <button
          type="button"
          className={mode === 'signup' ? 'active' : ''}
          onClick={() => setMode('signup')}
        >
          Sign up
        </button>
      </div>
      <label>
        Username
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          required
        />
      </label>
      <label>
        Password
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
          required
        />
      </label>
      {error && <p className="auth-error">{error}</p>}
      <button type="submit" disabled={busy}>
        {mode === 'login' ? 'Log in' : 'Create account'}
      </button>
    </form>
  )

  return (
    <div className="auth-control">
      {/* "/ Sign up" would only repeat the form's own tabs — the button just
          has to open the door. Matches the stub's "Log in to write this
          article" CTA. */}
      <button
        type="button"
        className="auth-link"
        onClick={() => onOpenChange(!open)}
      >
        Log in
      </button>
      {open &&
        (narrow ? (
          <>
            <div
              className="auth-overlay-backdrop"
              onClick={() => onOpenChange(false)}
            />
            {form}
          </>
        ) : (
          form
        ))}
    </div>
  )
}

export default AuthControl
