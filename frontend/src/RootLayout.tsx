import { Outlet } from 'react-router-dom'
import { useAuth } from './AuthContext'
import LoginModal from './components/LoginModal'

export default function RootLayout() {
  const { authStatus, authError, authLoading, handleLogin } = useAuth()

  if (authStatus === 'checking') {
    return null
  }

  if (authStatus === 'unauthenticated') {
    return <LoginModal onLogin={handleLogin} error={authError} loading={authLoading} />
  }

  return <Outlet />
}
