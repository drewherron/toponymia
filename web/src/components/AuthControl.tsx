import { useState } from 'react'
import type { FormEvent } from 'react'
import { fetchMe, login, logout, signup, verifyEmail } from '../api'
import type { User } from '../types'

interface AuthControlProps {
  user: User | null
  onUserChange: (user: User | null) => void
  open: boolean
  onOpenChange: (open: boolean) => void
  /** In the ☰ menu rather than the header bar: the form can't be a dropdown
   *  hanging off a dropdown, so it opens as a centred overlay instead. Keyed
   *  to the header's breakpoint (900), not the pane's (768). */
  inMenu: boolean
}

function AuthControl({
  user,
  onUserChange,
  open,
  onOpenChange,
  inMenu,
}: AuthControlProps) {
  const [mode, setMode] = useState<'login' | 'signup'>('login')
  // Signup is two steps under mandatory verification: the form, then the code
  // allauth emailed. 'verify' is only ever reached from a signup.
  const [step, setStep] = useState<'form' | 'verify'>('form')
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const switchMode = (next: 'login' | 'signup') => {
    setMode(next)
    setStep('form')
    setError(null)
  }

  const finish = (me: User | null) => {
    onUserChange(me)
    onOpenChange(false)
    setStep('form')
    setUsername('')
    setEmail('')
    setPassword('')
    setCode('')
  }

  const handleLogout = () => {
    logout()
      .then(() => onUserChange(null))
      .catch(console.error)
  }

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const done = () => fetchMe().then(finish)
    let work: Promise<void>
    if (step === 'verify') {
      work = verifyEmail(code).then(done)
    } else if (mode === 'signup') {
      work = signup(username, email, password).then((result) => {
        // Verification pending: keep the form open on the code step. The
        // session stays anonymous until the code lands, so don't fetchMe.
        if (result.verificationRequired) {
          setStep('verify')
          return
        }
        return done()
      })
    } else {
      work = login(username, password).then(done)
    }
    work
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

  const verifyStep = step === 'verify'

  const form = (
    <form
      className={`auth-form${inMenu ? ' auth-overlay' : ''}`}
      onSubmit={handleSubmit}
    >
      {verifyStep ? (
        <>
          <p className="auth-note">
            We emailed a verification code to <strong>{email}</strong>. Enter it
            to finish creating your account.
          </p>
          <label>
            Verification code
            <input
              className="auth-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoComplete="one-time-code"
              autoFocus
              required
            />
          </label>
        </>
      ) : (
        <>
          <div className="auth-tabs">
            <button
              type="button"
              className={mode === 'login' ? 'active' : ''}
              onClick={() => switchMode('login')}
            >
              Log in
            </button>
            <button
              type="button"
              className={mode === 'signup' ? 'active' : ''}
              onClick={() => switchMode('signup')}
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
          {mode === 'signup' && (
            <label>
              Email
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                required
              />
            </label>
          )}
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
        </>
      )}
      {error && <p className="auth-error">{error}</p>}
      <button type="submit" disabled={busy}>
        {verifyStep
          ? 'Verify'
          : mode === 'login'
            ? 'Log in'
            : 'Create account'}
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
        (inMenu ? (
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
