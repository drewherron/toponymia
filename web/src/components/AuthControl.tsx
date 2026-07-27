import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import {
  fetchMe,
  login,
  logout,
  requestPasswordReset,
  resetPassword,
  signup,
  verifyEmail,
} from '../api'
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
  // allauth emailed. 'verify' is only ever reached from a signup. Password
  // reset is the same shape from the other direction — 'forgot' asks for the
  // address, 'reset' takes the emailed code plus the new password — which is
  // why reset is by code and not by link: no step here needs a page of its own.
  const [step, setStep] = useState<'form' | 'verify' | 'forgot' | 'reset'>(
    'form',
  )
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  // Click-off to close. The ☰-menu overlay has its own backdrop, but the
  // header-bar dropdown had no dismiss but re-clicking "Log in" — easy to miss
  // once the panel is covering things. mousedown (not click) so a press that
  // starts outside closes before any inner button steals the click.
  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        onOpenChange(false)
      }
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [open, onOpenChange])

  const switchMode = (next: 'login' | 'signup') => {
    setMode(next)
    setStep('form')
    setError(null)
    setNotice(null)
  }

  const finish = (me: User | null) => {
    onUserChange(me)
    onOpenChange(false)
    setStep('form')
    setNotice(null)
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
    setNotice(null)
    const done = () => fetchMe().then(finish)
    let work: Promise<void>
    if (step === 'forgot') {
      // Never branch on whether the address exists — allauth answers the same
      // either way on purpose, and saying "no such account" would undo it.
      work = requestPasswordReset(email).then(() => {
        setStep('reset')
        setPassword('')
      })
    } else if (step === 'reset') {
      // The reset leaves the session anonymous, so hand back to the login tab
      // rather than calling fetchMe — there is no one logged in yet.
      work = resetPassword(code, password).then(() => {
        setStep('form')
        setMode('login')
        setPassword('')
        setCode('')
        setNotice('Password updated. Log in with your new password.')
      })
    } else if (step === 'verify') {
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
  const submitLabel = {
    verify: 'Verify',
    forgot: 'Send code',
    reset: 'Set password',
    form: mode === 'login' ? 'Log in' : 'Create account',
  }[step]

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
      ) : step === 'forgot' ? (
        <>
          {/* Reset is keyed by email even though login is by username, so ask
              for the address explicitly — the field above it wanted a name. */}
          <p className="auth-note">
            Enter the email address for your account and we'll send you a reset
            code.
          </p>
          <label>
            Email
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              autoFocus
              required
            />
          </label>
        </>
      ) : step === 'reset' ? (
        <>
          <p className="auth-note">
            If <strong>{email}</strong> has an account, a reset code is on its
            way. Enter it below with your new password.
          </p>
          <label>
            Reset code
            <input
              className="auth-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoComplete="one-time-code"
              autoFocus
              required
            />
          </label>
          <label>
            New password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
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
            {/* Signing up this is strictly the username (it becomes the public
                byline); logging in it is either identifier, and api.ts routes
                it to the right credential key. autocomplete="username" is
                right for both — it is the browser's login-identifier hint. */}
            {mode === 'login' ? 'Username or email' : 'Username'}
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
      {notice && <p className="auth-notice">{notice}</p>}
      {error && <p className="auth-error">{error}</p>}
      <button type="submit" disabled={busy}>
        {submitLabel}
      </button>
      {step === 'form' && mode === 'login' && (
        <button
          type="button"
          className="auth-alt"
          onClick={() => {
            setStep('forgot')
            setError(null)
            setNotice(null)
          }}
        >
          Forgot password?
        </button>
      )}
      {(step === 'forgot' || step === 'reset') && (
        <button
          type="button"
          className="auth-alt"
          onClick={() => switchMode('login')}
        >
          Back to log in
        </button>
      )}
    </form>
  )

  return (
    <div className="auth-control" ref={rootRef}>
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
