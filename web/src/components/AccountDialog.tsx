import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  changePassword,
  closeAccount,
  fetchMe,
  requestEmailChange,
  verifyEmail,
} from '../api'
import type { LegalDoc } from '../legal'
import type { User } from '../types'

interface AccountDialogProps {
  user: User
  onClose: () => void
  onUserChange: (user: User | null) => void
  onOpenDoc: (doc: LegalDoc) => void
}

type Panel = 'menu' | 'password' | 'email' | 'verify' | 'close'

const TITLES: Record<Panel, string> = {
  menu: 'Your account',
  password: 'Change password',
  email: 'Change email',
  verify: 'Confirm your new email',
  close: 'Close account',
}

/** Self-serve account management, opened from the username in the header.
 *  Password and email changes go to allauth's own endpoints; closing the
 *  account is ours (server/core/accounts.py). */
function AccountDialog({
  user,
  onClose,
  onUserChange,
  onOpenDoc,
}: AccountDialogProps) {
  const [panel, setPanel] = useState<Panel>('menu')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const go = (next: Panel) => {
    setPanel(next)
    setError(null)
    setNotice(null)
  }

  const run = (work: Promise<void>) => {
    setBusy(true)
    setError(null)
    work
      .catch((err: Error) => setError(err.message))
      .finally(() => setBusy(false))
  }

  const handlePassword = (event: FormEvent) => {
    event.preventDefault()
    run(
      changePassword(currentPassword, newPassword).then(() => {
        setCurrentPassword('')
        setNewPassword('')
        setPanel('menu')
        setNotice('Password changed.')
      }),
    )
  }

  const handleEmail = (event: FormEvent) => {
    event.preventDefault()
    // Nothing changes yet: allauth sends a code and stores the new address
    // only once it comes back.
    run(
      requestEmailChange(email).then(() => {
        setPanel('verify')
        setNotice(null)
      }),
    )
  }

  const handleVerify = (event: FormEvent) => {
    event.preventDefault()
    run(
      verifyEmail(code)
        .then(() => fetchMe())
        .then((me) => {
          onUserChange(me)
          setCode('')
          setEmail('')
          setPanel('menu')
          setNotice('Email address updated.')
        }),
    )
  }

  const handleClose = (event: FormEvent) => {
    event.preventDefault()
    run(
      closeAccount(password).then(() => {
        // The server has already ended the session.
        onUserChange(null)
        onClose()
      }),
    )
  }

  return (
    <div
      className="about-backdrop terms-backdrop"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="about-dialog account-dialog"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label={TITLES[panel]}
      >
        <div className="about-header">
          <h2>{TITLES[panel]}</h2>
          <button
            type="button"
            className="about-close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {notice && <p className="auth-notice">{notice}</p>}
        {error && <p className="auth-error">{error}</p>}

        {panel === 'menu' && (
          <>
            <dl className="account-facts">
              <dt>Username</dt>
              <dd>{user.username}</dd>
              <dt>Email</dt>
              <dd>{user.email}</dd>
            </dl>
            <p className="account-note">
              Your username is shown on everything you write. Edits and talk
              posts stay in the page history permanently — see the{' '}
              <button
                type="button"
                className="about-terms-link"
                onClick={() => onOpenDoc('terms')}
              >
                Terms of Use
              </button>{' '}
              and the{' '}
              <button
                type="button"
                className="about-terms-link"
                onClick={() => onOpenDoc('privacy')}
              >
                Privacy Policy
              </button>
              .
            </p>
            <div className="account-actions">
              <button type="button" onClick={() => go('password')}>
                Change password
              </button>
              <button type="button" onClick={() => go('email')}>
                Change email
              </button>
              <button
                type="button"
                className="account-danger"
                onClick={() => go('close')}
              >
                Close account
              </button>
            </div>
          </>
        )}

        {panel === 'password' && (
          <form className="account-form" onSubmit={handlePassword}>
            <label>
              Current password
              <input
                type="password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </label>
            <label>
              New password
              <input
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                autoComplete="new-password"
                required
              />
            </label>
            <div className="account-form-actions">
              <button type="submit" disabled={busy}>
                {busy ? 'Saving…' : 'Change password'}
              </button>
              <button type="button" onClick={() => go('menu')} disabled={busy}>
                Cancel
              </button>
            </div>
          </form>
        )}

        {panel === 'email' && (
          <form className="account-form" onSubmit={handleEmail}>
            <p className="account-note">
              We'll send a code to the new address. Your email only changes
              once you enter it.
            </p>
            <label>
              New email address
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                required
              />
            </label>
            <div className="account-form-actions">
              <button type="submit" disabled={busy}>
                {busy ? 'Sending…' : 'Send code'}
              </button>
              <button type="button" onClick={() => go('menu')} disabled={busy}>
                Cancel
              </button>
            </div>
          </form>
        )}

        {panel === 'verify' && (
          <form className="account-form" onSubmit={handleVerify}>
            <p className="account-note">
              We emailed a code to <strong>{email}</strong>. Enter it to
              finish the change.
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
            <div className="account-form-actions">
              <button type="submit" disabled={busy}>
                {busy ? 'Checking…' : 'Confirm'}
              </button>
              <button type="button" onClick={() => go('email')} disabled={busy}>
                Back
              </button>
            </div>
          </form>
        )}

        {panel === 'close' && (
          <form className="account-form" onSubmit={handleClose}>
            <p className="account-note">
              Closing your account removes your email address and signs you
              out for good. <strong>This cannot be undone.</strong>
            </p>
            <p className="account-note">
              Anything you have written stays on the site, credited to{' '}
              <code>[deleted]</code> instead of your username — the page
              history is how contributors are attributed, and it can't lose
              entries. Your username is retired at the same time, so nobody
              else can register it later — including you. If you've never
              written anything, the account is removed outright and the name
              stays available.
            </p>
            <label>
              Confirm your password
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </label>
            <div className="account-form-actions">
              <button
                type="submit"
                className="account-danger"
                disabled={busy}
              >
                {busy ? 'Closing…' : 'Close my account'}
              </button>
              <button type="button" onClick={() => go('menu')} disabled={busy}>
                Cancel
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}

export default AccountDialog
