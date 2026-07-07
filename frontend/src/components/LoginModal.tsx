import { useRef, useEffect, type FormEvent } from 'react'
import './LoginModal.css'

interface LoginModalProps {
  onLogin: (password: string) => Promise<void>
  error: string | null
  loading: boolean
}

export default function LoginModal({ onLogin, error, loading }: LoginModalProps) {
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (loading) return

    const password = inputRef.current?.value ?? ''
    void onLogin(password)
  }

  return (
    <div className="login-overlay" role="dialog" aria-modal="true">
      <form className="login-card" onSubmit={handleSubmit}>
        <h1 className="login-title">QA Agent</h1>

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
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.preventDefault()
            }
          }}
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
    </div>
  )
}
