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

| Route                                          | Page                       | Purpose                                                                                                                         |
| ---------------------------------------------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `/`                                            | `SprintListPage`           | All sprints; cards link to detail or test-environment pages                                                                     |
| `/sprints/new`                                 | `CreateSprintPage`         | Create a sprint (with inline repo registration + README status)                                                                 |
| `/sprints/:id`                                 | `SprintDetailPage`         | Enter requirements manually or upload a PRD to split into requirements; follow analysis via polling, answer/confirm/confirm all |
| `/sprints/:id/test-environment`                | `TestEnvironmentPage`      | Describe test environment access, answer clarifications, confirm                                                                |
| `/sprints/:id/test-plans`                      | `TestPlansPage`            | Generate, review, give feedback on, edit, and approve/approve all test plans                                                    |
| `/sprints/:id/test-runs`                       | `TestRunsPage`             | Two lists: exploratory sessions and scripted test runs; start either                                                            |
| `/sprints/:id/test-runs/:runId`                | `TestRunDetailPage`        | Per-requirement, per-case execution results; download scripts; restart failed executions                                        |
| `/sprints/:id/exploratory-runs/:runId`         | `ExploratoryRunDetailPage` | One requirement's exploration: summary, finding counts, session list; restart or retry the summary                              |
| `/sprints/:id/exploratory-sessions/:sessionId` | `ExploratorySessionPage`   | SBTM session sheet: charter, SFDIPOT areas, actions used, notes, findings with screenshots, action log                          |
| `/repos`                                       | `RepoListPage`             | Registered repos; deactivate unused ones                                                                                        |

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
