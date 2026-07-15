import { createBrowserRouter } from 'react-router-dom'
import CreateSprintPage from './pages/CreateSprintPage'
import RepoListPage from './pages/RepoListPage'
import SprintDetailPage from './pages/SprintDetailPage'
import SprintListPage from './pages/SprintListPage'
import TestEnvironmentPage from './pages/TestEnvironmentPage'
import RootLayout from './RootLayout'

export const routes = [
  {
    element: <RootLayout />,
    children: [
      { path: '/', element: <SprintListPage /> },
      { path: '/sprints/new', element: <CreateSprintPage /> },
      { path: '/sprints/:id', element: <SprintDetailPage /> },
      { path: '/sprints/:id/test-environment', element: <TestEnvironmentPage /> },
      { path: '/repos', element: <RepoListPage /> },
    ],
  },
]

export const router = createBrowserRouter(routes)
