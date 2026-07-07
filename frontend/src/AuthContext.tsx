/* eslint-disable react-refresh/only-export-components */

import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { checkAuthStatus, verifyPassword } from './services/api'

export type AuthStatus = 'checking' | 'authenticated' | 'unauthenticated'

export interface AuthContextValue {
  authStatus: AuthStatus
  authError: string | null
  authLoading: boolean
  handleLogin: (password: string) => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
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
          setAuthLoading(false)
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

  return (
    <AuthContext.Provider value={{ authStatus, authError, authLoading, handleLogin }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return ctx
}
