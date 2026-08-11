import { useRef, type FormEvent } from 'react'
import ModalShell from './ModalShell'
import './LoginModal.css'

/**
 * The shared-password gate. `RootLayout` renders this *instead of* the outlet,
 * so there is no page behind it — which is why it passes no `onClose`: Escape
 * and the backdrop have nowhere to dismiss to.
 *
 * It is on `ModalShell` like every other dialog. The shell supplies the focus
 * trap and the initial focus (the first focusable child is the password input,
 * which is what this used to arrange for itself).
 */

interface LoginModalProps {
  onLogin: (password: string) => Promise<void>
  error: string | null
  loading: boolean
}

export default function LoginModal({ onLogin, error, loading }: LoginModalProps) {
  const inputRef = useRef<HTMLInputElement>(null)

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (loading) return

    const password = inputRef.current?.value ?? ''
    void onLogin(password)
  }

  return (
    <ModalShell title="QA Agent" cardClassName="login-card">
      <form className="login-form" onSubmit={handleSubmit}>
        <label htmlFor="login-password" className="login-label">
          Access Code
        </label>
        <input
          id="login-password"
          ref={inputRef}
          type="password"
          className="login-input"
          placeholder="Enter access code"
          autoComplete="off"
        />

        {error && (
          <p className="login-error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" className="login-submit" disabled={loading}>
          {loading ? 'Verifying…' : 'Submit'}
        </button>
      </form>
    </ModalShell>
  )
}
