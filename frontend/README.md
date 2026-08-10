# QA Agent Frontend

React + TypeScript + Vite frontend for the QA Agent.

## Quick Start

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

## Pages

Routes defined in `src/router.tsx`, all under `RootLayout` (the auth gate):

| Route                                          | Page                       | Purpose                                                                                                                                                                                                                     |
| ---------------------------------------------- | -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/`                                            | `SprintListPage`           | All sprints; cards link to detail or test-environment pages                                                                                                                                                                 |
| `/sprints/new`                                 | `CreateSprintPage`         | Create a sprint (with inline repo registration + README status)                                                                                                                                                             |
| `/sprints/:id`                                 | `SprintDetailPage`         | Enter requirements manually or upload a PRD to split into requirements; follow analysis via polling, answer/confirm/confirm all                                                                                             |
| `/sprints/:id/test-environment`                | `TestEnvironmentPage`      | Describe test environment access, answer clarifications, confirm                                                                                                                                                            |
| `/sprints/:id/test-plans`                      | `TestPlansPage`            | Generate, review, give feedback on, edit, and approve/approve all test plans                                                                                                                                                |
| `/sprints/:id/test-runs`                       | `TestRunsPage`             | QA metrics panel over two lists: exploratory sessions and scripted test runs; start either. Also holds the issue-tracker panel — connect, change, or disconnect Jira / GitHub Issues for the sprint                         |
| `/sprints/:id/test-runs/:runId`                | `TestRunDetailPage`        | Per-requirement, per-case execution results; failed and errored cases show a finding card (same one exploratory sessions use); download scripts; restart failed executions; file or retry bug findings to the issue tracker |
| `/sprints/:id/exploratory-runs/:runId`         | `ExploratoryRunDetailPage` | One requirement's exploration: summary, finding counts, session list; restart or retry the summary; file or retry bug findings to the issue tracker                                                                         |
| `/sprints/:id/exploratory-sessions/:sessionId` | `ExploratorySessionPage`   | SBTM session sheet: charter, SFDIPOT areas, actions used, notes, findings with screenshots, action log; polls while the session is queued or exploring                                                                      |
| `/repos`                                       | `RepoListPage`             | Registered repos; deactivate unused ones                                                                                                                                                                                    |

## Shared modules

Behaviour every page needs lives in one place rather than per page. Reach for these before writing a new copy:

| Module                  | What it owns                                                                                                                                                      |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `hooks/usePolling.ts`   | The 2.5 s refresh interval, the in-flight guard, and `EXPORT_GRACE_TICKS` — the bounded extra polling that covers findings being filed after a run reads terminal |
| `hooks/useAsyncData.ts` | Load-on-mount with the cancellation guard; returns `{ data, loading, error, setData }`                                                                            |
| `hooks/useAction.ts`    | One-shot mutations: `busy`, `error`, and a `run(promise, also?)` that clears the error when a new attempt starts                                                  |
| `outdated.ts`           | `isOutdated(run)` — staleness derived client-side from `outdated_reasons`                                                                                         |
| `exportState.ts`        | `awaitingExport(run)` — whether a completed run's findings are probably still being filed                                                                         |
| `statusLabels.ts`       | How each status reads, keyed by its status union so a missing label is a type error                                                                               |
| `format.ts`             | `plural`, `formatDate`, `formatDateTime`                                                                                                                          |
| `styles/controls.css`   | `.btn*`, `.badge*`, `.run-badge*`, `.session-badge*`, `.back-link(s)` — imported once from `App.tsx`; colocated `.css` files stay page-local                      |

`FindingCard`, `ExportSummary`, `OutdatedBadge` and `RestartControl` are source-agnostic: scripted and exploratory results render through the same components.

## QA metrics

`SprintMetricsPanel` sits above both run lists on the test-runs page, fed by `GET /api/sprints/:id/qa-metrics` and refreshed on the same poll as the lists. Four tiles: test cases run, exploratory sessions, distinct bugs, and defect density — plus a per-requirement breakdown ordered worst-first, with archived requirements marked `(deleted)`. Only the bug figure is a distinct-defect count; the issue count beside it is raw, because an issue reports obstructed testing rather than a defect (see `backend/README.md`).

Every figure is computed in Python. Nothing here divides one number by another (Convention #10): the definition living in two places is what `test_plans_missing` exists to remember, and the ticket-based bug collapse cannot be done client-side at all.

Four things the panel deliberately says rather than smooths over:

- **Test cases are reported at two levels.** The headline is _distinct_ cases; executions sit beneath it, labelled. A case run three times adds 1 to the first and 3 to the second, and the density figure uses the first — otherwise re-running an unfixed plan would make the sprint read healthier.
- **Density has three readings, not two.** `—` means nothing was tested, `0.00` means tested and clean, and `< 0.01` means tested with a real but tiny defect rate. "0.00 bugs / case" on a sprint that found bugs is the one output the tile must never produce.
- **Excluded runs are named.** Only completed runs are counted, so a run still going or one that failed is reported as excluded rather than silently dropped.
- **Coverage is two facts, not a fraction.** The tile reads `5 requirements covered` and `7 current requirements`, never `5 of 7`. Coverage is what runs already did, while the total is a live snapshot — so a covered requirement that is later edited (back to `analyzing`) or deleted puts covered _above_ total, and `1 of 0 requirements covered` would read as a broken panel. "currently" is what makes that case self-explaining.

A footnote explains rows summing above the headline — one defect can affect several requirements — and appears only when they actually differ.

## Issue tracker

The test-runs page carries a panel for connecting the sprint to a **Jira project** or a **GitHub Issues repo**. Both run modals then offer a "File bug findings to …" checkbox, checked by default when a tracker is connected and disabled when none is — connecting one is itself the statement that findings should go there, so the modal does not ask twice.

Choosing GitHub Issues offers **"Use this sprint's repository"**, ticked by default (the sprint is already bound to a repo, and its stored access token is used unless a token is typed). Unticking it restores the free-text `owner/repo` field; it starts unticked only when a saved connection already points at a different repository, since that was a deliberate choice. The repository is derived server-side from the registered GitHub link — `IssueTrackerModal` takes the sprint's `repo` purely to label the checkbox and to word the token hint from `has_access_token`.

A run's detail page shows what its findings did: how many bugs became how many tickets, each ticket listed with the number of findings it stands for, and a button to file the rest. Filing happens after the run is marked finished, so both detail pages keep polling for a couple of minutes after a **completed** run's bugs are still unfiled — otherwise the page would stop refreshing mid-export and read "not yet filed" until reloaded. That button is the **normal** path for any run that did not finish — a superseded run, or one a finished sprint swept, arrives with its bugs unfiled by design, and reads "File N bugs" rather than "Retry". Individual finding cards link their ticket, mark themselves `(grouped)` when they were filed under another finding's ticket, and show the filing error inline when there is one.

Every run shows its id — `Run #14` in the list rows and in the detail headers. That is the number a filed ticket quotes ("Scripted run 14", "Exploratory run 10"), and it is otherwise only in the URL, so a reader holding the ticket has nothing to match against. It is a global identifier rather than a per-sprint count, which is why it reads `#14` rather than "the 14th run".

## Environment Variables

| Variable        | Default                 | Description          |
| --------------- | ----------------------- | -------------------- |
| `VITE_API_BASE` | `http://localhost:8000` | Backend API base URL |

Define in `frontend/.env` (see `.env.example`). In local dev the Vite server also proxies `/api` → `http://localhost:8000` (`vite.config.ts`), so `VITE_API_BASE` is optional.

## Login Flow

When `APP_PASSWORD` is set on the backend, the frontend shows a full-screen login modal:

1. On page load, `GET /api/auth/check` checks whether a valid `qa_auth` cookie already exists.
2. If `valid: true` — the modal is skipped, the app appears immediately.
3. If `valid: false` — a password input is shown.
4. On submit, `POST /api/auth/verify` validates the password. On success the backend sets an HttpOnly session cookie; on failure an inline error is displayed.
5. The cookie is sent automatically by the browser on every subsequent API request — the API functions in `src/services/api.ts` need no per-call auth handling.

If `APP_PASSWORD` is not set on the backend, all auth checks return `{ valid: true }` and the modal never appears.

## Scripts

| Command              | Purpose                       |
| -------------------- | ----------------------------- |
| `npm run dev`        | Start Vite dev server         |
| `npm run build`      | Type-check + production build |
| `npm run lint`       | ESLint                        |
| `npm test`           | Vitest                        |
| `npm run test:watch` | Vitest in watch mode          |
| `npm run preview`    | Preview production build      |
