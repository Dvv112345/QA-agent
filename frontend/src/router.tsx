import { createBrowserRouter } from 'react-router-dom'
import CicdPage from './pages/CicdPage'
import CreateSprintPage from './pages/CreateSprintPage'
import ExploratoryRunDetailPage from './pages/ExploratoryRunDetailPage'
import NonfunctionalRunDetailPage from './pages/NonfunctionalRunDetailPage'
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
import { STAGES, type StageId } from './stages'

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

/*
 * The pipeline stages, in the order a sprint is walked through them, built from
 * the one definition in `stages.ts` so the bar and `StageNav` cannot disagree.
 *
 * A stage's chain names every stage it came through, not just its URL parent:
 * `/sprints/7/test-runs` is a sibling of `/sprints/7/test-plans` as far as the
 * path is concerned, but you only arrive at runs by way of the environment and
 * the plans, and the trail is what says how far along the sprint is. The sprint
 * crumb doubles as the first stage, which is why there is no separate
 * Requirements crumb.
 */
function stageCrumb(id: StageId): CrumbSpec {
  return { id, label: STAGES[id].label, path: STAGES[id].pattern }
}

const TEST_ENV = stageCrumb('test-environment')
const TEST_PLANS = stageCrumb('test-plans')
const TEST_RUNS = stageCrumb('test-runs')

function crumbs(...chain: CrumbSpec[]) {
  return { crumbs: chain }
}

/*
 * A stage page: the trail behind it, plus the stages ahead of it.
 *
 * The forward stages render dimmed until their gate opens, so the bar shows the
 * whole sequence and how far along the sprint is rather than stopping wherever
 * the user happens to be. Only the four stage pages get one — a run detail page
 * is inside a stage, not at one, and forward stages there would read as
 * siblings of the run.
 */
function stagePage(chain: CrumbSpec[], ahead: CrumbSpec[]) {
  return { crumbs: chain, forward: ahead }
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
        handle: stagePage([SPRINTS, SPRINT], [TEST_ENV, TEST_PLANS, TEST_RUNS]),
      },
      {
        path: '/sprints/:id/test-environment',
        element: <TestEnvironmentPage />,
        handle: stagePage([SPRINTS, SPRINT, TEST_ENV], [TEST_PLANS, TEST_RUNS]),
      },
      {
        path: '/sprints/:id/test-plans',
        element: <TestPlansPage />,
        handle: stagePage([SPRINTS, SPRINT, TEST_ENV, TEST_PLANS], [TEST_RUNS]),
      },
      {
        // The terminal stage — nothing ahead of it.
        path: '/sprints/:id/test-runs',
        element: <TestRunsPage />,
        handle: crumbs(SPRINTS, SPRINT, TEST_ENV, TEST_PLANS, TEST_RUNS),
      },
      {
        path: '/sprints/:id/test-runs/:runId',
        element: <TestRunDetailPage />,
        handle: crumbs(SPRINTS, SPRINT, TEST_ENV, TEST_PLANS, TEST_RUNS, {
          id: 'run',
          label: 'Run',
        }),
      },
      {
        path: '/sprints/:id/exploratory-runs/:runId',
        element: <ExploratoryRunDetailPage />,
        handle: crumbs(SPRINTS, SPRINT, TEST_ENV, TEST_PLANS, TEST_RUNS, {
          id: 'run',
          label: 'Exploratory Run',
        }),
      },
      {
        path: '/sprints/:id/nonfunctional-runs/:runId',
        element: <NonfunctionalRunDetailPage />,
        handle: crumbs(SPRINTS, SPRINT, TEST_ENV, TEST_PLANS, TEST_RUNS, {
          id: 'run',
          label: 'Nonfunctional Run',
        }),
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
          TEST_ENV,
          TEST_PLANS,
          TEST_RUNS,
          { id: 'run', label: 'Exploratory Run', path: '/sprints/:id/test-runs' },
          { id: 'session', label: 'Session Sheet' },
        ),
      },
      {
        /*
         * A side door off the last stage rather than a stage of its own —
         * deliberately absent from `stages.ts`, which is the one definition
         * of the four *gated* pipeline stages. Exporting is optional, has no
         * gate of its own beyond the runs that produced the scripts, and
         * adding it there would put it in the forward-crumb sequence as
         * though a sprint were incomplete without it.
         */
        path: '/sprints/:id/cicd',
        element: <CicdPage />,
        handle: crumbs(SPRINTS, SPRINT, TEST_ENV, TEST_PLANS, TEST_RUNS, {
          id: 'cicd',
          label: 'CI/CD',
        }),
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
