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
import type { CrumbSpec } from './components/Breadcrumb'

/*
 * Breadcrumb structure lives here, next to the routes it describes, so two
 * pages at the same depth cannot disagree about their ancestry. The routes are
 * flat rather than nested — `useMatches()` therefore cannot walk upward, and
 * each route spells out its own chain.
 *
 * Labels here are the fallback. A page that has loaded its data publishes
 * something specific through `useCrumb`; one that has not, or cannot, keeps
 * these and still navigates correctly.
 */

const SPRINTS: CrumbSpec = { id: 'sprints', label: 'Sprints', path: '/' }
const SPRINT: CrumbSpec = { id: 'sprint', label: 'Sprint', path: '/sprints/:id' }
const TEST_RUNS: CrumbSpec = {
  id: 'test-runs',
  label: 'Test Runs',
  path: '/sprints/:id/test-runs',
}

function crumbs(...chain: CrumbSpec[]) {
  return { crumbs: chain }
}

export const routes = [
  {
    element: <RootLayout />,
    children: [
      {
        path: '/',
        element: <SprintListPage />,
        handle: crumbs(SPRINTS),
      },
      {
        path: '/sprints/new',
        element: <CreateSprintPage />,
        handle: crumbs(SPRINTS, { id: 'new-sprint', label: 'New Sprint' }),
      },
      {
        path: '/sprints/:id',
        element: <SprintDetailPage />,
        handle: crumbs(SPRINTS, SPRINT),
      },
      {
        path: '/sprints/:id/test-environment',
        element: <TestEnvironmentPage />,
        handle: crumbs(SPRINTS, SPRINT, { id: 'test-environment', label: 'Test Environment' }),
      },
      {
        path: '/sprints/:id/test-plans',
        element: <TestPlansPage />,
        handle: crumbs(SPRINTS, SPRINT, { id: 'test-plans', label: 'Test Plans' }),
      },
      {
        path: '/sprints/:id/test-runs',
        element: <TestRunsPage />,
        handle: crumbs(SPRINTS, SPRINT, { id: 'test-runs', label: 'Test Runs' }),
      },
      {
        path: '/sprints/:id/test-runs/:runId',
        element: <TestRunDetailPage />,
        handle: crumbs(SPRINTS, SPRINT, TEST_RUNS, { id: 'run', label: 'Run' }),
      },
      {
        path: '/sprints/:id/exploratory-runs/:runId',
        element: <ExploratoryRunDetailPage />,
        handle: crumbs(SPRINTS, SPRINT, TEST_RUNS, { id: 'run', label: 'Exploratory Run' }),
      },
      {
        // The parent exploratory run is not in this URL — it comes from the
        // fetched session's `exploratory_run_id`. The page publishes both the
        // label and the target for that crumb; until it loads, the crumb falls
        // back to the run list, which is still somewhere useful to go.
        path: '/sprints/:id/exploratory-sessions/:sessionId',
        element: <ExploratorySessionPage />,
        handle: crumbs(
          SPRINTS,
          SPRINT,
          TEST_RUNS,
          { id: 'run', label: 'Exploratory Run', path: '/sprints/:id/test-runs' },
          { id: 'session', label: 'Session Sheet' },
        ),
      },
      {
        path: '/repos',
        element: <RepoListPage />,
        handle: crumbs(SPRINTS, { id: 'repos', label: 'Repositories' }),
      },
    ],
  },
]

export const router = createBrowserRouter(routes)
