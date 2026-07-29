import { createBrowserRouter } from 'react-router-dom'
import CreateSprintPage from './pages/CreateSprintPage'
import ExploratoryRunDetailPage from './pages/ExploratoryRunDetailPage'
import ExploratorySessionPage from './pages/ExploratorySessionPage'
import RepoListPage from './pages/RepoListPage'
import SprintDetailPage from './pages/SprintDetailPage'
import SprintListPage from './pages/SprintListPage'
import TestEnvironmentPage from './pages/TestEnvironmentPage'
import TestPlansPage from './pages/TestPlansPage'
import TestRunDetailPage from './pages/TestRunDetailPage'
import TestRunsPage from './pages/TestRunsPage'
import RootLayout from './RootLayout'

export const routes = [
  {
    element: <RootLayout />,
    children: [
      { path: '/', element: <SprintListPage /> },
      { path: '/sprints/new', element: <CreateSprintPage /> },
      { path: '/sprints/:id', element: <SprintDetailPage /> },
      { path: '/sprints/:id/test-environment', element: <TestEnvironmentPage /> },
      { path: '/sprints/:id/test-plans', element: <TestPlansPage /> },
      { path: '/sprints/:id/test-runs', element: <TestRunsPage /> },
      { path: '/sprints/:id/test-runs/:runId', element: <TestRunDetailPage /> },
      {
        path: '/sprints/:id/exploratory-runs/:runId',
        element: <ExploratoryRunDetailPage />,
      },
      {
        path: '/sprints/:id/exploratory-sessions/:sessionId',
        element: <ExploratorySessionPage />,
      },
      { path: '/repos', element: <RepoListPage /> },
    ],
  },
]

export const router = createBrowserRouter(routes)
