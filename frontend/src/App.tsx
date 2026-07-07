import { useState, useEffect, useCallback } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import HomePage from './pages/HomePage'
import LoadingPage from './pages/LoadingPage'
import LoginModal from './components/LoginModal'
import { checkAuthStatus, verifyPassword } from './services/api'
import './App.css'

type AuthStatus = 'checking' | 'authenticated' | 'unauthenticated'

export default function App() {
  const [authStatus, setAuthStatus] = useState<AuthStatus>('checking')
  const [authError, setAuthError] = useState<string | null>(null)
  const [authLoading, setAuthLoading] = useState(false)

  useEffect(() => {
    checkAuthStatus()
      .then((result) => {
        setAuthStatus(result.valid ? 'authenticated' : 'unauthenticated')
      })
      .catch(() => {
        // If /check fails, assume unauthenticated and show the modal
        setAuthStatus('unauthenticated')
      })
  }, [])

  const handleLogin = useCallback((password: string): Promise<void> => {
    setAuthLoading(true)
    setAuthError(null)

    return verifyPassword(password)
      .then((result) => {
        if (result.valid) {
          setAuthStatus('authenticated')
        } else {
          setAuthError('Incorrect access code')
          setAuthLoading(false)
        }
      })
      .catch(() => {
        setAuthError('Something went wrong. Please try again.')
        setAuthLoading(false)
      })
  }, [])

  if (authStatus === 'checking') {
    return null
  }

  if (authStatus === 'unauthenticated') {
    return <LoginModal onLogin={handleLogin} error={authError} loading={authLoading} />
  }

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/loading" element={<LoadingPage />} />
      </Routes>
    </BrowserRouter>
  )
}
