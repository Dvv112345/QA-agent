import { createBrowserRouter } from 'react-router-dom'
import HomePage from './pages/HomePage'
import LoadingPage from './pages/LoadingPage'
import RootLayout from './RootLayout'

export const routes = [
  {
    element: <RootLayout />,
    children: [
      { path: '/', element: <HomePage /> },
      { path: '/loading', element: <LoadingPage /> },
    ],
  },
]

export const router = createBrowserRouter(routes)
