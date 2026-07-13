# CLAUDE.md

## Project Overview

QA Agent is a full-stack application for managing QA analysis of GitHub repositories. Users register GitHub repos, then create **sprints** linked to a repo. Creating a sprint downloads the repo's README (or accepts a user-uploaded one) plus a filtered file-tree listing and stores them for analysis. Within a sprint, users enter **requirements** that an LLM checks for QA-clarity via Redis/RQ background workers, with a clarification question/answer loop per requirement (see [Requirement analysis](#key-flows)). It consists of a FastAPI + PostgreSQL backend and a React + Vite frontend.

## Tech Stack

| Layer    | Technology                                                                                                                 |
| -------- | -------------------------------------------------------------------------------------------------------------------------- |
| Backend  | Python 3.10+, FastAPI, Uvicorn, SQLModel, PostgreSQL (psycopg2), httpx, cryptography, python-dotenv, RQ, Redis, OpenAI SDK |
| Frontend | React 19, TypeScript 6, Vite 8, React Router 7, ESLint 10, Prettier 3, Vitest                                              |
| Dev      | pre-commit (Ruff, Prettier, ESLint, general hooks), pytest, pytest-asyncio, pytest-httpx                                   |

## Commands

All backend commands run from the repo root; frontend commands from `frontend/`.

| Command                            | Purpose                                                     |
| ---------------------------------- | ----------------------------------------------------------- |
| `pip install -e ".[dev]"`          | Install backend + dev dependencies (pytest, ruff, …)        |
| `python -m backend.main`           | Start API server at http://localhost:8000 (docs at `/docs`) |
| `python -m backend.worker`         | Start an RQ worker (required for requirement analysis)      |
| `python -m backend.clear_queue`    | Empty the RQ queue and job registries                       |
| `python -m pytest -v`              | Run all backend tests                                       |
| `python -m pytest -k <keyword> -v` | Run backend tests matching a keyword                        |
| `npm install`                      | Install frontend dependencies                               |
| `npm run dev`                      | Vite dev server with HMR at http://localhost:5173           |
| `npm run build`                    | Type-check (`tsc -b`) and build for production              |
| `npm run lint`                     | ESLint                                                      |
| `npm test` / `npm run test:watch`  | Vitest (single run / watch mode)                            |
| `pre-commit install`               | Install git hooks (first time only)                         |
| `pre-commit run --all-files`       | Run all hooks on all files                                  |

**Prerequisites for running the backend:** PostgreSQL running with a `qa_agent` database (`createdb qa_agent`), and `backend/.env` present (copy from `backend/.env.example`). Tables are auto-created on startup by `init_db()` — no migrations. Requirement analysis additionally needs Redis, a worker (`python -m backend.worker`, second terminal), and an LLM API key (`OPENAI_API_KEY`); without them requirement rows simply stay `pending`. Tests need none of these (see [Testing](#testing)).

## Architecture

### Backend (`backend/`)

| Path                     | Purpose                                                                                                   |
| ------------------------ | --------------------------------------------------------------------------------------------------------- |
| `main.py`                | App factory (`create_app()`), CORS, global exception handler, `/api/health`, lifespan (reconciler), CLI   |
| `config.py`              | Typed env-var helpers (`_get_bool`, `_get_int`, `_get_list`, `_get_optional_path`) + all config constants |
| `database.py`            | SQLAlchemy engine, `get_session()` FastAPI dependency, `new_session()` for workers, `init_db()`           |
| `models/database.py`     | Table models: `Repo`, `Sprint`, `Requirement` + `RequirementStatus` (repo → sprints → requirements)       |
| `models/types.py`        | Request/response SQLModel types (`RepoResponse`, `SprintResponse`, `RequirementResponse`, …)              |
| `routes/auth.py`         | `POST /api/auth/verify`, `GET /api/auth/check` (unauthenticated)                                          |
| `routes/repos.py`        | Create / list / deactivate repos, README status check                                                     |
| `routes/sprints.py`      | Create / list / get / finish sprints (captures repo file tree on create)                                  |
| `routes/requirements.py` | Batch create, list/poll, answer, confirm, edit, restart, delete requirements                              |
| `services/storage.py`    | `StorageService` — persists sprint READMEs to disk when `STORE_OFFLINE=true`                              |
| `services/queue.py`      | `QueueService` (RQ wrapper): `enqueue_analysis`, `get_job`; lazy singleton via `get_queue_service()`      |
| `services/llm.py`        | OpenAI-SDK client wrapper: `check_clarity`, `revise_requirement`, `ClarityResult`, `LLMError`             |
| `services/reconciler.py` | `reconcile_once` + `reconciler_loop` — re-enqueues lost jobs, sweeps stale worker heartbeats              |
| `tasks/`                 | RQ task modules, one file per task type (`analyze_requirement.py`); enqueued by dotted string path        |
| `worker.py`              | RQ worker CLI (`python -m backend.worker`; SimpleWorker on Windows)                                       |
| `utils/auth.py`          | `verify_auth(request)` dependency (cookie check)                                                          |
| `utils/crypto.py`        | Fernet encrypt/decrypt for GitHub tokens (needs `ENCRYPTION_KEY`)                                         |
| `utils/github_utils.py`  | GitHub API client: URL parsing, metadata, README download, file-tree fetch, typed `GitHubError` hierarchy |
| `utils/sprint_utils.py`  | Unique sprint directory generation (DB + filesystem checked, TOCTOU-safe)                                 |
| `clear_queue.py`         | CLI to empty the RQ queue and job registries                                                              |
| `tests/`                 | pytest suite (see [Testing](#testing))                                                                    |

### Frontend (`frontend/src/`)

| Path              | Purpose                                                                                                                                                     |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `main.tsx`        | Entry — renders `<App />` in StrictMode                                                                                                                     |
| `App.tsx`         | `<AuthProvider>` wrapping `<RouterProvider>`                                                                                                                |
| `router.tsx`      | `createBrowserRouter`: `/` SprintListPage, `/sprints/new` CreateSprintPage, `/sprints/:id` SprintDetailPage, `/repos` RepoListPage — all under `RootLayout` |
| `RootLayout.tsx`  | Auth gate: `checking` → null, `unauthenticated` → `LoginModal`, else `<Outlet />`                                                                           |
| `AuthContext.tsx` | `useAuth()` — auth status state machine + `handleLogin`                                                                                                     |
| `pages/`          | One file per page + colocated `.css` and `.test.tsx`                                                                                                        |
| `components/`     | `LoginModal` (full-screen, uncancellable), `RequirementForm` (dynamic rows), `RequirementCard` (per-status)                                                 |
| `services/api.ts` | All fetch calls; shared `handleResponse<T>()` parses FastAPI `detail` errors                                                                                |
| `types.ts`        | Interfaces mirroring `backend/models/types.py`                                                                                                              |
| `test/`           | Vitest setup + `renderWithRouter` helper                                                                                                                    |

### Key flows

**Repo registration** (`POST /api/repos`): validate GitHub URL → fetch metadata from GitHub API (verifies accessibility) → encrypt optional access token with Fernet → persist. Deactivation is a soft delete that clears the token and is blocked while active sprints reference the repo.

**Sprint creation** (`POST /api/sprints`): validate name + repo → refresh repo metadata from GitHub → best-effort file-tree capture onto `Repo.file_tree` (filtered + size-capped; failures log a warning, never block) → resolve README (user upload wins over GitHub download; 422 if neither) → generate unique directory under `STORAGE_LOCATION` → save README if `STORE_OFFLINE=true` → persist sprint. Frontend `CreateSprintPage` supports inline repo creation, shows README status before submitting, and navigates to `/sprints/:id` on success.

**Requirement analysis** (`POST /api/sprints/{id}/requirements` + worker): rows are created `pending` and enqueued best-effort (one RQ job per requirement, arg = requirement id only). The worker task is state-driven: initial clarity check, or — when `clarifying_question` + `pending_answer` are set — a revision that rewrites the description. Lifecycle: `pending → analyzing → needs_clarification ⇄ analyzing → ready → confirmed`, plus `failed` with a Restart action. PostgreSQL is the sole status of record (Redis is transport only); the frontend polls `GET /api/sprints/{id}/requirements` every 2.5 s while rows are in progress. Clarification is capped at `MAX_CLARIFICATION_ROUNDS` (3); past the cap only confirm-as-is or manual edit. `confirmed` is content-terminal (all mutations 422) but rows can still be deleted — in any status. Finishing a sprint marks its `pending`/`analyzing` requirements `failed` (nothing analyzes on a finished sprint; the worker task and reconciler skip inactive sprints as a backstop). The reconciler (asyncio task in the app lifespan) re-enqueues `pending` backlog when Redis recovers and sweeps `analyzing` rows with stale `last_heartbeat` (crashed worker) back to `pending`, marking them `failed` after `MAX_AUTO_RETRIES`. LLM prompts include the sprint README (stored copy → re-download → none) and `Repo.file_tree` as context.

## Configuration

All backend config is environment variables loaded via `python-dotenv` in `backend/config.py`. **`backend/.env.example` is the single source of truth for the variable list** — copy it to `backend/.env` and adjust. Highlights:

| Variable                                          | Notes                                                                                                                                                                |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DATABASE_URL`                                    | PostgreSQL connection string (default `postgresql://postgres:postgres@localhost:5432/qa_agent`)                                                                      |
| `ENCRYPTION_KEY`                                  | Fernet key, **required** to register repos with access tokens. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `APP_PASSWORD`                                    | Shared UI password; unset = auth disabled                                                                                                                            |
| `STORE_OFFLINE` / `STORAGE_LOCATION`              | Persist sprint READMEs to disk when `"true"` (default `./uploads`, git-ignored)                                                                                      |
| `CORS_ORIGINS`                                    | Default `http://localhost:5173`                                                                                                                                      |
| `GITHUB_API_TIMEOUT`                              | Seconds for GitHub API requests (default 15)                                                                                                                         |
| `REDIS_*`, `JOB_*`                                | Redis connection + RQ job timeout/TTL for the analysis queue                                                                                                         |
| `OPENAI_API_KEY`                                  | **Required for requirement analysis.** Never logged or returned in responses                                                                                         |
| `OPENAI_BASE_URL` / `OPENAI_MODEL`                | Any OpenAI-compatible provider; defaults target DeepSeek (`https://api.deepseek.com`, `deepseek-v4-flash`)                                                           |
| `MAX_CLARIFICATION_ROUNDS`                        | Q&A rounds per requirement before the cap (default 3)                                                                                                                |
| `MAX_AUTO_RETRIES`                                | Automatic retries before a requirement is marked `failed` (default 3)                                                                                                |
| `RECONCILER_INTERVAL` / `HEARTBEAT_STALE_SECONDS` | Reconciler tick (30 s) and crashed-worker threshold (180 s — keep above `OPENAI_TIMEOUT`)                                                                            |

Frontend: `VITE_API_BASE` (in `frontend/.env`, default example `http://localhost:8000`). Vite only exposes vars prefixed `VITE_`. The dev server also proxies `/api` → `http://localhost:8000` (`vite.config.ts`) to avoid CORS locally.

## Authentication

Shared-secret password gate backed by an HttpOnly session cookie. Optional — when `APP_PASSWORD` is unset the app is fully open.

- `backend/utils/auth.py` — `verify_auth(request)` dependency: reads `qa_auth` cookie, compares via `secrets.compare_digest()`, raises 401 on mismatch, passes everything when `APP_PASSWORD` unset.
- `backend/routes/auth.py` — unauthenticated: `POST /api/auth/verify` (sets cookie on match), `GET /api/auth/check` (reports state, never 401s).
- Protected: `/api/repos*`, `/api/sprints*`, and `/api/requirements*` routers use `dependencies=[Depends(verify_auth)]`. `/api/health` and `/api/auth/*` are open.
- Cookie: `qa_auth=<password>; Path=/; SameSite=Strict; HttpOnly` (session-scoped).
- Frontend: `AuthContext` runs `checking` → `authenticated`/`unauthenticated` on mount via `GET /api/auth/check`; `RootLayout` shows `LoginModal` until authenticated. The browser sends the cookie automatically, so API functions need no auth handling.

## Testing

### Backend (`backend/tests/`, pytest)

- `conftest.py` has an **autouse fixture that replaces the PostgreSQL engine with in-memory SQLite** (`sqlite:///file:test_db?mode=memory&cache=shared&uri=true`) and no-ops `init_db` — no test ever touches a real database. It also sets a fresh `ENCRYPTION_KEY`, disables dotenv, and deletes `SSL_CERT_FILE` (a Windows footgun that breaks httpx client creation).
- `async_client` fixture: httpx `AsyncClient` over `ASGITransport`, auth disabled (`APP_PASSWORD` deleted), Redis health check mocked, `get_session` overridden to the SQLite session.
- A second **autouse fixture (`_isolate_redis`) no-ops `QueueService._connect`** — no test can enqueue to live Redis; the degraded service returns `None` from `enqueue_analysis`. Tests that assert enqueue behaviour monkeypatch `get_queue_service` with a recording stub.
- GitHub API calls are mocked with **pytest-httpx** (`httpx_mock` fixture); `conftest.pytest_collection_modifyitems` relaxes its assert-all-consumed defaults. Use the shared `_create_repo(client, url, httpx_mock)` helper to seed repos through the API.
- LLM calls never hit the network: `test_llm.py` stubs `_get_client`; task/route tests monkeypatch `services.llm` functions. The task and reconciler are tested as plain functions against the SQLite fixture (`new_session` picks up the patched engine); intermediate statuses are seeded directly via `db_session` (see `_seed_sprint`/`_seed_requirement` in `test_requirement_routes.py`).
- The reconciler loop starts via FastAPI lifespan, which httpx `ASGITransport` never runs — it is inert in API tests; `reconcile_once` is unit-tested directly.
- Auth-specific tests (`test_auth_routes.py`) build their own client with `APP_PASSWORD=secret123`.

### Frontend (Vitest + Testing Library)

- Config lives in `vite.config.ts` under the `test` key (jsdom, `globals: true`, setup file imports `@testing-library/jest-dom/vitest`).
- Tests are colocated with source (`*.test.tsx` next to the component).
- `src/test/test-utils.tsx` exports `renderWithRouter(ui, { initialEntries? })` — wraps the component in `createMemoryRouter` + `RouterProvider` with a catch-all route.
- Mock the API module (`vi.mock('../services/api')`) rather than raw `fetch` where possible; mock `useNavigate` to assert navigation.

## Gotchas

| Gotcha                                   | Solution                                                                                                                                                    |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`location.state` in `useEffect` deps** | `location.state` is a new reference each render → infinite re-runs. Depend on specific properties instead.                                                  |
| **MemoryRouter route state format**      | `initialEntries` must be `[{ pathname: '/x', state: {...} }]`, not `['/x', { state }]`.                                                                     |
| **CI `npm ci` fails on peer deps**       | Lockfile `"peer": true` markers from a different npm version break `npm ci`. The CI workflow runs `npm install --package-lock-only` first to normalize.     |
| **Empty optional form fields**           | Browsers send empty strings for blank optional form fields — backend routes downgrade `""` to `None` (see `access_token` in `routes/repos.py`).             |
| **Windows `SSL_CERT_FILE`**              | May point at a missing file and break httpx; `github_utils.py` uses a certifi-based SSL context, and conftest deletes the var for tests.                    |
| **Sprint directory uniqueness**          | Never generate directories with check-then-create; `generate_sprint_directory` uses `os.makedirs(exist_ok=False)` in a retry loop to avoid the TOCTOU race. |
| **Token security**                       | GitHub tokens are Fernet-encrypted at rest, never returned in responses (`RepoResponse` omits the field) and never logged. Keep it that way.                |

### Redis / RQ (active — requirement analysis)

RQ workers process requirement-analysis jobs (revived from the removed word-count pipeline around commit `ab337c1`). Hard-won lessons, now applied in code:

- **Windows has no `os.fork()`**: `worker.py` uses `SimpleWorker` when `sys.platform == "win32"`. `QueueService._connect` sets a 5 s socket timeout for enqueue-side Redis calls (enqueue, ping, job lookups); RQ itself bumps the worker connection's timeout above its blocking-dequeue window on startup, so dead-connection hangs are bounded on both sides.
- **RQ's `job_timeout` hard-kill also needs fork**: it stays as a Linux-side backstop; on Windows the analysis task's only long wait is the LLM call, which `OPENAI_TIMEOUT` bounds. Crashed/hung workers are caught by the DB heartbeat + reconciler instead.
- **One `QueueService` per process**: lazy module-level singleton (`get_queue_service()` / `reset_queue_service()`), never constructed per-request. Degrades gracefully (`enqueue_analysis` returns `None`) when Redis is down — rows stay `pending` and the reconciler enqueues them on recovery.
- **Tasks are enqueued by dotted string path** (`backend.tasks.analyze_requirement.analyze_requirement_task`), so the web process never imports task modules. Task modules must not import from `backend.services.queue` or `backend.worker` (circular-import rule).
- **Redis is transport only**: no `job.meta` status; PostgreSQL rows carry the whole lifecycle, making every enqueue idempotent (the task no-ops on rows that aren't `pending`/`analyzing`).

## CI/CD

Both workflows trigger on `pull_request` to **`main`**:

| Workflow    | File                                | Steps                                                                                             |
| ----------- | ----------------------------------- | ------------------------------------------------------------------------------------------------- |
| Backend CI  | `.github/workflows/backend_ci.yml`  | Python 3.12 → `pip install -e ".[dev]"` → `pytest` (no lint step — Ruff runs via pre-commit only) |
| Frontend CI | `.github/workflows/frontend_ci.yml` | Node 22 + npm cache → lockfile sync → `npm ci` → lint → build → test                              |

## Pre-commit Quality Gates

Ruff (lint + format) for Python; Prettier + ESLint for frontend; general hooks (trailing whitespace, EOF, YAML/TOML checks, large files 500KB, merge conflicts, private keys). Do not skip hooks unless explicitly asked.

## MCP Servers

| Server              | When to use                                                                                                       |
| ------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `code-review-graph` | Code exploration, reviews, impact analysis. **Always call `get_minimal_context_tool` first.** See section below.  |
| `context7`          | Up-to-date docs for any library/framework (React, FastAPI, Vite, …). Use even when you think you know the answer. |
| `github`            | PRs, issues, code search. Call `get_me` first. For PR reviews use the pending-review workflow.                    |

## Skills

| Skill                          | Purpose                                                                |
| ------------------------------ | ---------------------------------------------------------------------- |
| `code-review`                  | Review code files in a specified scope and provide fix recommendations |
| `verify`                       | Run the app and observe behavior to confirm a change works             |
| `simplify`                     | Review changed code for reuse, simplification, efficiency; apply fixes |
| `workflows-plan`               | Create detailed implementation plans for features                      |
| `workflows-work`               | Execute an approved implementation plan with progress tracking         |
| `deep-research`                | Multi-source, fact-checked research reports with citations             |
| `gstack-qa` / `gstack-qa-only` | QA workflow (full stack / QA only)                                     |
| `gstack-review`                | Review a pull request                                                  |
| `review`                       | Review a pull request                                                  |
| `security-review`              | Security-focused code review                                           |

## Conventions and Notes

1. **Working directory**: repo root for backend commands; `cd frontend` for frontend commands.
2. **Python imports**: `backend.`-prefixed (`from backend.config import ...`). Always run modules as `python -m backend.main` from the repo root, never `python backend/main.py`.
3. **Planning docs**: feature plans live in `thoughts/plans/`, implementation tracking in `thoughts/implementations/` (git-ignored).
4. **Response types**: every endpoint declares a `response_model` from `models/types.py`; table models stay in `models/database.py`.
5. **Error handling**: raise `HTTPException` with a helpful `detail`; the global handler catches everything else as a 500. GitHub failures map to 422 (validation-time) or 502 (fetch-time).
6. **Frontend error parsing**: FastAPI `detail` can be a string or `[{ msg }]` — `handleResponse` in `services/api.ts` handles both; route all fetches through it.
7. **Ports**: backend 8000, frontend 5173.
8. **Commit messages / PR descriptions**: do not mention that they were written by Claude.

## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool                        | Use when                                               |
| --------------------------- | ------------------------------------------------------ |
| `detect_changes`            | Reviewing code changes — gives risk-scored analysis    |
| `get_review_context`        | Need source snippets for review — token-efficient      |
| `get_impact_radius`         | Understanding blast radius of a change                 |
| `get_affected_flows`        | Finding which execution paths are impacted             |
| `query_graph`               | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes`     | Finding functions/classes by name or keyword           |
| `get_architecture_overview` | Understanding high-level codebase structure            |
| `refactor_tool`             | Planning renames, finding dead code                    |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
