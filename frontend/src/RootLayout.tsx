import { Outlet } from 'react-router-dom'
import { useAuth } from './AuthContext'
import { BreadcrumbProvider } from './BreadcrumbContext'
import Breadcrumb from './components/Breadcrumb'
import LoginModal from './components/LoginModal'
import ScrollToTop from './components/ScrollToTop'

export default function RootLayout() {
  const { authStatus, authError, authLoading, handleLogin } = useAuth()

  if (authStatus === 'checking') {
    return null
  }

  if (authStatus === 'unauthenticated') {
    return <LoginModal onLogin={handleLogin} error={authError} loading={authLoading} />
  }

  // Chrome renders only inside the authenticated branch — above the outlet, so
  // it survives each page's loading and not-found early returns, and never
  // appears over the login modal.
  return (
    <BreadcrumbProvider>
      {/* One column for the chrome and the page alike — see `.page-frame`.
          Nesting them is what guarantees they share a left edge. */}
      <div className="page-frame">
        <Breadcrumb />
        <Outlet />
      </div>
      <ScrollToTop />
    </BreadcrumbProvider>
  )
}
