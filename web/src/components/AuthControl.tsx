import { useState } from 'react'
import type { FormEvent } from 'react'
import { fetchMe, login, logout, signup } from '../api'
import type { User } from '../types'

interface AuthControlProps {
  user: User | null
  onUserChange: (user: User | null) => void
  open: boolean
  onOpenChange: (open: boolean) => void
}

function AuthControl({ user, onUserChange, open, onOpenChange }: AuthControlProps) {
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

  return (
    <div className="auth-control">
      <button
        type="button"
        className="auth-link"
        onClick={() => onOpenChange(!open)}
      >
        Log in / Sign up
      </button>
      {open && (
        <form className="auth-form" onSubmit={handleSubmit}>
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
              autoComplete={
                mode === 'login' ? 'current-password' : 'new-password'
              }
              required
            />
          </label>
          {error && <p className="auth-error">{error}</p>}
          <button type="submit" disabled={busy}>
            {mode === 'login' ? 'Log in' : 'Create account'}
          </button>
        </form>
      )}
    </div>
  )
}

export default AuthControl
