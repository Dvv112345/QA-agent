import { RouterProvider } from 'react-router-dom'
import { AuthProvider } from './AuthContext'
import { router } from './router'
// Before App.css and every page stylesheet, so a page can still override a
// shared control if it genuinely needs to.
import './styles/controls.css'
import './App.css'

export default function App() {
  return (
    <AuthProvider>
      <RouterProvider router={router} />
    </AuthProvider>
  )
}
