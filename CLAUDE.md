# CLAUDE.md

## Project Overview

QA Agent is a full-stack application that accepts source code (zip archives) and requirement documents (markdown) for automated Quality Assurance analysis. It consists of a FastAPI backend and a React + Vite frontend.

## Tech Stack

| Layer     | Technology                                                         |
| --------- | ------------------------------------------------------------------ |
| Backend   | Python 3.10+, FastAPI, Uvicorn, SQLModel, python-dotenv, RQ, Redis |
| Frontend  | React 19, TypeScript 6, Vite 8, ESLint 10, Prettier 3              |
| Dev tools | pre-commit (Ruff, Prettier, ESLint, general hooks), pytest, httpx  |

## Build and Test Commands

### Backend (run from repo root)

```bash
# Install dependencies
pip install -r backend/requirements.txt

# Start the API server (http://localhost:8000)
python -m backend.main

# Start the RQ worker (separate terminal, required for word-count processing)
# Only needed when STORE_OFFLINE=true
python -m backend.worker

# Interactive API docs
# Open http://localhost:8000/docs
```

### Frontend (run from `frontend/`)

```bash
cd frontend

# Install dependencies
npm install

# Start dev server with HMR (http://localhost:5173)
npm run dev

# Type-check and build for production
npm run build

# Lint
npm run lint

# Preview production build
npm run preview
```

### Backend Tests (run from repo root)

```bash
# Install all dependencies including dev tools
pip install -e ".[dev]"

# Run all tests
python -m pytest -v

# Run a single test file
python -m pytest backend/tests/test_upload.py -v

# Run tests matching a keyword
python -m pytest -k "health" -v
```

Test fixtures are in `backend/tests/conftest.py`. Tests use `pytest` with `pytest-asyncio` (`asyncio_mode = auto`) and `httpx` for async HTTP testing against the FastAPI app via `ASGITransport`. Sample files (`sample_zip.zip`, `sample_md.md`) live alongside the tests.

### Pre-commit hooks (run from repo root)

```bash
# Install hooks (first time only)
pre-commit install

# Run all hooks manually on staged files
pre-commit run

# Run all hooks on all files
pre-commit run --all-files

# Run a single hook
pre-commit run ruff --all-files
pre-commit run prettier --all-files
```

## Directory Structure

```
QA-agent/
├── CLAUDE.md                           # This file — project guide for Claude Code
├── pyproject.toml                       # Project metadata, dependencies (runtime + dev), pytest + ruff config
├── .pre-commit-config.yaml             # Pre-commit hooks: Ruff, Prettier, ESLint, general checks
├── .gitignore                          # Git ignores: thoughts/, .env, __pycache__, venv/, uploads/, dist/
├── .claude/
│   └── settings.local.json             # Local Claude Code permissions and enabled MCP servers
├── .github/
│   └── workflows/
│       ├── backend_ci.yml              # Backend CI: lint + test on PRs to master
│       └── frontend_ci.yml             # Frontend CI: lint + build + test on PRs to master
├── backend/
│   ├── README.md                       # Backend-specific docs: quick start, env vars, API endpoints
│   ├── requirements.txt                # Python dependencies: fastapi, uvicorn, python-multipart, sqlmodel, dotenv, rq, redis
│   ├── .env.example                    # Template for environment variables with documentation
│   ├── .env                            # Actual environment variables (git-ignored)
│   ├── main.py                         # FastAPI app factory, CORS, exception handlers, health endpoint, CLI entry point
│   ├── config.py                       # Typed env-var helpers (_get_bool, _get_int, _get_list, _get_optional_path)
│   ├── tasks.py                        # RQ task functions (count_words_task — no circular imports)
│   ├── worker.py                       # RQ worker CLI entry point (python -m backend.worker)
│   ├── tests/
│   │   ├── __init__.py
│   │   ├── conftest.py                  # Fixtures: async_client, sample_zip_bytes, sample_md_bytes
│   │   ├── sample_zip.zip               # Sample zip for upload tests
│   │   ├── sample_md.md                 # Sample markdown for upload tests
│   │   ├── test_config.py               # Config module tests
│   │   ├── test_jobs.py                 # Job status endpoint tests
│   │   ├── test_main.py                 # create_app, health, CORS, exception handler tests
│   │   ├── test_queue.py                # QueueService tests
│   │   ├── test_routes.py               # Upload route tests
│   │   ├── test_storage.py              # StorageService tests
│   │   ├── test_word_utils.py           # Word-count utility tests
│   │   └── test_zip_utils.py            # Zip extraction and path traversal tests
│   ├── models/
│   │   ├── __init__.py
│   │   └── types.py                    # SQLModel types: HealthResponse, UploadResponse, JobStatusResponse, FileWordCount
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── jobs.py                      # GET /api/jobs/{job_id}/status — word-count progress + results
│   │   └── upload.py                   # POST /api/upload — accepts zip + markdown, validates, extracts, returns tree
│   ├── services/
│   │   ├── __init__.py
│   │   ├── queue.py                     # QueueService — Redis-backed job queue (enqueue + status lookup)
│   │   └── storage.py                  # StorageService — conditional disk persistence when STORE_OFFLINE=true
│   └── utils/
│       ├── __init__.py
│       ├── word_utils.py                # is_text_file() + count_words_in_file() helpers
│       └── zip_utils.py                # Safe zip extraction (path traversal protection, streaming I/O, tree rendering)
├── frontend/
│   ├── README.md                       # Vite + React template README
│   ├── .gitignore                      # Frontend-specific ignores: node_modules, dist, .vscode, .idea, .env
│   ├── .env.example                    # Template: VITE_API_BASE=http://localhost:8000
│   ├── package.json                    # Dependencies and scripts (dev, build, lint, test, preview)
│   ├── .prettierrc                     # Prettier config: no semis, single quotes, trailing commas, 100 print width
│   ├── eslint.config.js                # ESLint flat config: JS recommended, TS recommended, React hooks, React refresh
│   ├── index.html                      # HTML entry point with <div id="root">
│   ├── vite.config.ts                  # Vite config + Vitest test block (jsdom, globals, setupFiles)
│   ├── tsconfig.json                   # Root TS config — references tsconfig.app.json and tsconfig.node.json
│   ├── tsconfig.app.json               # App TS config: ES2023, bundler mode, react-jsx, strict linting
│   ├── tsconfig.node.json              # Node TS config: ES2023, bundler mode (for vite.config.ts)
│   ├── public/
│   │   ├── favicon.svg                 # Site favicon
│   │   └── icons.svg                   # SVG sprite sheet
│   └── src/
│       ├── main.tsx                    # React entry point — renders <App /> into #root with StrictMode
│       ├── App.tsx                     # BrowserRouter + Routes: / → HomePage, /loading → LoadingPage
│       ├── App.css                     # App-level styles (minimal — each page owns its layout)
│       ├── App.test.tsx                # Route rendering tests (MemoryRouter + Routes, not wrapping App)
│       ├── index.css                   # Global tokens (CSS custom props), reset, dark mode, typography
│       ├── types.ts                    # Shared interfaces (UploadResponse matching backend models/types.py)
│       ├── pages/
│       │   ├── HomePage.tsx            # File selection + extension validation, no API call
│       │   ├── HomePage.css
│       │   ├── HomePage.test.tsx        # 6 tests — inputs, validation errors, navigation
│       │   ├── LoadingPage.tsx          # Upload orchestration, polling, progress/results coordination
│       │   ├── LoadingPage.css
│       │   └── LoadingPage.test.tsx     # 12 tests — upload, polling, progress, results, error states
│       ├── components/
│       │   ├── WordCountProgress.tsx    # Progress bar (computed %) + "Waiting for worker" pulse
│       │   ├── WordCountProgress.test.tsx # 9 tests — pulse, progress bar, percentage computation, aria
│       │   ├── WordCountResult.tsx      # Switch on job status → FinishedResult | error | unavailable
│       │   ├── WordCountResult.test.tsx   # 8 tests — finished, failed, unknown, queued/started → null
│       │   ├── FinishedResult.tsx        # Requirements doc table + source files table + total
│       │   └── FinishedResult.test.tsx    # 7 tests — rendering, sorting, empty array, null totals, locale
│       ├── services/
│       │   ├── api.ts                  # uploadFiles() — FormData + fetch, VITE_API_BASE env var
│       │   └── api.test.ts             # 4 tests — mocked fetch (200, 422, network error, FormData)
│       └── test/
│           ├── setup.ts                # Vitest setup — @testing-library/jest-dom/vitest matchers
│           └── test-utils.tsx           # renderWithRouter(ui, { initialEntries? }) — MemoryRouter wrapper
```

## Code Patterns

### Configuration

All backend config comes from environment variables, loaded via `python-dotenv` in `backend/config.py`. Each variable has a typed getter (`_get_bool`, `_get_int`, `_get_list`, `_get_optional_path`) with sensible defaults. Copy `.env.example` to `.env` to customize.

Key RQ / Redis config variables:

| Variable         | Default     | Description                                                         |
| ---------------- | ----------- | ------------------------------------------------------------------- |
| `REDIS_HOST`     | `localhost` | Redis server hostname                                               |
| `REDIS_PORT`     | `6379`      | Redis server port                                                   |
| `REDIS_PASSWORD` | (none)      | Redis auth password (set to `QaPassword` in `.env.example` for dev) |
| `REDIS_DB`       | `0`         | Redis database number                                               |
| `JOB_TIMEOUT`    | `300`       | Maximum job execution time in seconds (RQ hard kill)                |
| `JOB_RESULT_TTL` | `3600`      | Seconds before job results expire from Redis (1 hour)               |

### API Patterns

- **FastAPI app factory**: `create_app()` in `main.py` wires up middleware, routers, exception handlers, and health checks.
- **Health check**: `GET /api/health` returns `{ status, storage, redis }`. `storage` reports `"memory_only"`, `"available"`, or `"unavailable: <reason>"`. `redis` reports `"available"` or `"unavailable: <reason>"` — useful for monitoring Redis connectivity alongside disk storage.
- **Exception handling**: A global `Exception` handler catches unexpected errors, re-raises `HTTPException` for FastAPI to handle normally, and returns 500 for everything else.
- **Validation**: Route handlers validate file extensions, content magic bytes, size limits, and encoding before processing.
- **Response types**: All responses use `SQLModel` types defined in `models/types.py`.

### Frontend Patterns

- **Strict TypeScript**: `noUnusedLocals`, `noUnusedParameters`, `erasableSyntaxOnly`, `noFallthroughCasesInSwitch` are all enabled.
- **Flat ESLint config**: Uses the new ESLint flat config format (`eslint/config`'s `defineConfig`).
- **Vite with Oxc**: Uses `@vitejs/plugin-react` which leverages Oxc for fast transforms.
- **StrictMode**: React StrictMode is enabled in `main.tsx`.
- **Component extraction**: When a page component accumulates distinct visual sections with their own state-dependent rendering logic (progress bars, result tables, error states), extract those sections into focused components under `src/components/`. Each extracted component receives only the props it needs via a typed interface. This keeps page files manageable, makes each sub-view independently testable, and surfaces the component's contract through its props type. During the `rq-setup` implementation, `LoadingPage.tsx` shed ~180 lines by extracting `WordCountProgress`, `WordCountResult`, and `FinishedResult` — the page stayed focused on orchestration (fetch, poll, coordinate) while the components handled pure rendering. Each component gets its own `*.test.tsx` colocated next to it, testing all render states directly through props rather than indirectly through the parent page's fetch mocking and polling timers.

#### Routing (React Router v7)

- **Two-page app**: `BrowserRouter` + `Routes` in `App.tsx` maps `/` → `HomePage`, `/loading` → `LoadingPage`.
- **File passing via route state**: `File` objects are structured-cloneable, so they survive `navigate('/loading', { state: { zipFile, mdFile } })`. No serialization needed.
- **Missing-state guard**: LoadingPage checks for `state?.zipFile` / `state?.mdFile` and shows a fallback message with a link back to `/` if files are missing (direct navigation to `/loading`).

#### API Layer

- **Fetch + FormData**: Built-in browser APIs — no HTTP library. `FormData` with fields `zip_file` and `markdown_file` match backend `UploadFile` parameter names.
- **API base via Vite env var**: `import.meta.env.VITE_API_BASE` (default `http://localhost:8000`). Define in `.env`, document in `.env.example`. Vite only exposes vars prefixed with `VITE_`.
- **Error parsing**: 422 FastAPI validation errors return `{ detail: string | [{ msg: string }] }`. Parse both forms for user-friendly messages.

#### Frontend Testing (Vitest + Testing Library)

- **Config**: Vitest config lives inside `vite.config.ts` under the `test` key (no separate `vitest.config.ts`). Uses `jsdom` environment, `globals: true`, and a setup file that imports `@testing-library/jest-dom/vitest` matchers.
- **Test utils**: `src/test/test-utils.tsx` exports `renderWithRouter(ui, { initialEntries? })` — wraps `render` with `MemoryRouter`. Pass `initialEntries` as `[{ pathname: '/loading', state: { ... } }]` to simulate route state.
- **Tests colocated with source**: `api.test.ts` next to `api.ts`, `HomePage.test.tsx` next to `HomePage.tsx`, etc.
- **API mocking**: Mock global `fetch` with `vi.spyOn(globalThis, 'fetch')` or mock the API module with `vi.mock('../services/api')`.
- **Navigation assertions**: Mock `useNavigate` from `react-router-dom` with `vi.fn()`, or assert `<Link>` `href` attributes directly.

### Common Gotchas

<!-- prettier-ignore -->
| Gotcha | Explanation | Solution |
| ------ | ----------- | -------- |
| **Nested Router error in tests** | Wrapping `<App />` (which contains `<BrowserRouter>`) inside `<MemoryRouter>` throws "You cannot render a `<Router>` inside another `<Router>`." | Test routes directly: render `<MemoryRouter><Routes><Route path="/" element={<HomePage />} />...` instead of testing `<App />`. |
| **MemoryRouter route state format** | `initialEntries` must be `[{ pathname: '/loading', state: { ... } }]`, NOT `['/loading', { state: ... }]`. | Always use the object form: `{ pathname: string, state?: unknown }`. |
| **Box-drawing chars break `getByText`** | `├──`, `└──`, `│` in `tree_text` output aren't matched by Testing Library's text queries. | Use `document.querySelector('pre.tree-text')` and assert on `.textContent` instead. |
| **`useEffect` dep on `location.state`** | `location.state` is a new object reference every render, causing infinite effect re-runs. | Depend on specific state properties (`state?.zipFile`, `state?.mdFile`) instead of the whole `state` object. Add an eslint-disable comment with explanation. |
| **Retry via `retryKey` counter** | Calling `uploadFiles()` directly in a click handler duplicates logic and bypasses effect cleanup. | Use a `retryKey` state counter as a useEffect dependency — increment it to re-trigger the effect. |
| **CI `npm ci` fails on peer deps** | `package-lock.json` can contain `"peer": true` markers that `npm ci` in a fresh environment rejects. | Add `npm install --package-lock-only` step before `npm ci` in the CI workflow to normalize the lockfile. |
| **`/// <reference types="vitest" />`** | This triple-slash directive in `vite.config.ts` is unnecessary when Vitest provides its own types via the `test` config key. | Remove it — the `test` block in `defineConfig` is sufficient for TypeScript to infer Vitest types. |
| **Direct `QueueService()` construction** | Constructing `QueueService()` directly in route modules creates duplicate Redis connections and loses the shared singleton. | Always use `get_queue_service()` from `backend.services.queue` — it returns the process-wide singleton. Call `reset_queue_service()` if Redis recovers from an outage. |
| **RQ Worker hangs / won't stop on Windows** | RQ's default `Worker` class uses `os.fork()`, which doesn't exist on Windows. Without a fork, the worker process ignores SIGTERM/SIGINT and must be forcefully killed. Additionally, the default Redis connection has no socket timeout, so the worker can hang indefinitely on a dead connection. | Use `SimpleWorker` instead of `Worker` on Windows (`sys.platform == "win32"`). Set `conn.socket_timeout` (5s recommended) so the worker detects dead connections quickly. These mitigations are handled in `backend/worker.py`. |
| **RQ Queue job_timeout does not work for Windows** | RQ's hard-kill `job_timeout` also relies on forking, so on Windows a **cooperative timeout** in the task itself is required — `count_words_task` checks `time.monotonic() - start > timeout` between each file and raises `TimeoutError` to self-terminate gracefully. | Pass `JOB_TIMEOUT` as both the cooperative timeout argument to the task and as RQ's `job_timeout` for cross-platform coverage. An example of this mitigation is in `backend/tasks.py`. |

### Pre-commit Quality Gates

All staged files are auto-formatted and linted before commit:

- **Python**: Ruff lint (with auto-fix) + Ruff format
- **Frontend**: Prettier + ESLint (with auto-fix)
- **General**: trailing whitespace removal, EOF newlines, merge conflict detection, large file check (500KB), private key detection, YAML/TOML validation

### CI/CD (GitHub Actions)

Both workflows trigger on `pull_request` to `master`:

| Workflow    | File                                | Steps                                                                                                        |
| ----------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Backend CI  | `.github/workflows/backend_ci.yml`  | checkout → Python 3.13 → pip install (dev) → lint (Ruff) → test (pytest)                                     |
| Frontend CI | `.github/workflows/frontend_ci.yml` | checkout → Node 22 + npm cache → sync lockfile → npm ci → lint (ESLint) → build (tsc + Vite) → test (Vitest) |

**Frontend CI pattern**: Run `npm install --package-lock-only` before `npm ci` to normalize the lockfile — this prevents `npm ci` failures when the lockfile has peer dependency metadata from a different npm version.

### File Upload Flow

1. Client POSTs zip + markdown to `/api/upload`
2. Route validates extensions (`.zip`, `.md`/`.markdown`)
3. Reads files into memory, validates magic bytes (ZIP header) and UTF-8 encoding
4. Generates a job ID (`YYYYMMDD-HHMMSS-<32 hex chars>`)
5. If `STORE_OFFLINE=true`, persists to `STORAGE_LOCATION/<job_id>/`
6. Extracts zip to temp directory (or reads from stored path), builds directory tree + file-only path list
7. If `STORE_OFFLINE=true` and files were persisted, enqueues a word-count RQ job (returns `word_count_enqueued=true`)
8. Returns `UploadResponse` with job_id, status, filenames, tree (list + text), and `word_count_enqueued` flag

### Word-Count Flow (RQ)

1. Frontend receives `word_count_enqueued=true` → starts polling `GET /api/jobs/{job_id}/status` every 5s
2. RQ worker picks up the `count_words_task` from the `qa-jobs` queue
3. Worker counts words in markdown first, then each zip file
4. Text-vs-binary detection: checks MIME type first (fast, no I/O), then falls back to null-byte scan (first 8 KB). Known binary extensions (`.png`, `.zip`, `.exe`, etc.) are skipped without reading.
5. Progress: `job.meta['processed_files']` incremented after each file; frontend computes `%` from `total_files`
6. On completion: frontend displays two result sections — requirements document + source files table with totals
7. Frontend stops polling when status is `finished`, `failed`, or `unknown`
8. Job results expire from Redis after `JOB_RESULT_TTL` seconds (default 3600 = 1 hour)

**Running multiple workers**: For production workloads with concurrent uploads, start multiple worker processes. Each worker handles one job at a time, so N workers = N concurrent word-count jobs:

```bash
# Terminal 1
python -m backend.worker

# Terminal 2
python -m backend.worker

# Or via a process manager (supervisord, systemd, Docker):
# supervisord example:
# [program:qa-worker]
# command=python -m backend.worker
# numprocs=4
# process_name=qa-worker-%(process_num)s
```

**QueueService singleton**: All consumers must obtain the shared `QueueService` instance via `get_queue_service()` (defined in `backend/services/queue.py`) rather than constructing `QueueService()` directly. This ensures only one Redis connection pool exists per process. Call `reset_queue_service()` to discard the cached singleton and reconnect after a transient Redis outage.

## CLI Commands

| Command                                   | Purpose                                             |
| ----------------------------------------- | --------------------------------------------------- |
| `python -m backend.main`                  | Start the FastAPI dev server on port 8000           |
| `python -m backend.worker`                | Start RQ worker (separate terminal, Redis required) |
| `pip install -r backend/requirements.txt` | Install/update Python runtime dependencies          |
| `pip install -e ".[dev]"`                 | Install Python dev dependencies (pytest, etc)       |
| `python -m pytest -v`                     | Run all backend tests                               |
| `python -m pytest -k <keyword> -v`        | Run tests matching a keyword                        |
| `cd frontend && npm install`              | Install/update frontend dependencies                |
| `cd frontend && npm run dev`              | Start Vite dev server with HMR on port 5173         |
| `cd frontend && npm run build`            | Type-check and build frontend for production        |
| `cd frontend && npm run lint`             | Lint frontend with ESLint                           |
| `cd frontend && npm test`                 | Run all frontend tests (Vitest)                     |
| `cd frontend && npm run test:watch`       | Run frontend tests in watch mode                    |
| `cd frontend && npm run preview`          | Preview production build locally                    |
| `pre-commit install`                      | Install git pre-commit hooks                        |
| `pre-commit run --all-files`              | Run all pre-commit hooks on all files               |
| `pre-commit run <hook-id> --all-files`    | Run a specific hook on all files                    |

## MCP Servers

| Server              | Purpose                                                                                                                                                        | When to Use                                                                                                                                                                                                                    |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `code-review-graph` | Persistent knowledge graph for token-efficient, context-aware code reviews. Parses code with Tree-sitter, builds a structural graph, provides impact analysis. | For code reviews, understanding blast radius of changes, finding architectural hotspots, detecting dead code, refactoring. Always call `get_minimal_context_tool` first. Graph already built at `.code-review-graph/graph.db`. |
| `context7`          | Up-to-date library documentation lookup.                                                                                                                       | Always use when asked about any library, framework, SDK, API, or CLI tool — especially React, FastAPI, Vite, TypeScript, etc. Use even when you think you know the answer.                                                     |
| `github`            | GitHub platform interaction — issues, PRs, repos, code search, file management.                                                                                | For creating/reading PRs, managing issues, searching code, push files, branch operations. Use `get_me` first to understand permissions. For PR reviews, use the pending review workflow: create → add comments → submit.       |

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

## Additional Notes and Behaviors

1. **Working directory**: All commands should be run from the repo root (`C:\Users\DVV11\Documents\Microsoft internship\QA-agent`). Frontend commands should `cd frontend` first.

2. **Python path**: The backend uses `backend.` prefixed imports (e.g., `from backend.config import ...`). Run `python -m backend.main` from the repo root so the package is importable. Do not run `python backend/main.py` directly.

3. **Environment file**: Before running the backend, ensure `backend/.env` exists. Copy from `backend/.env.example` if missing. The env file is git-ignored.

4. **Uploads directory**: `backend/uploads/` is git-ignored. Uploaded files are stored here when `STORE_OFFLINE=true`.

5. **Code review workflow**: When reviewing changes, always use the `code-review-graph` MCP server. Start with `get_minimal_context_tool`, then use `get_review_context_tool` or `detect_changes_tool` for detailed analysis. Use `get_knowledge_gaps_tool` to identify structural weaknesses.

6. **Pre-commit formatting**: All Python code is auto-formatted with Ruff, all frontend code with Prettier. The pre-commit hook runs on every commit. Do not skip hooks unless explicitly asked.

7. **Frontend linting**: ESLint runs in the pre-commit hook but can also be run manually via `npm run lint`. The config uses the flat config format with TypeScript-ESLint.

8. **Tests**:
   - **Backend**: Tests live in `backend/tests/` and run with `pytest` via `python -m pytest -v`. Dev dependencies (pytest, pytest-asyncio, httpx) are declared in `pyproject.toml` under `[project.optional-dependencies] dev`. Install with `pip install -e ".[dev]"`. CI runs on every PR to `master` via `.github/workflows/backend_ci.yml`.
   - **Frontend**: Tests live next to the component they test (`*.test.ts`/`*.test.tsx`). Run with `npm test` (Vitest). Uses `jsdom` environment, `@testing-library/react`, and `@testing-library/jest-dom` matchers. A shared `renderWithRouter` wrapper (in `src/test/test-utils.tsx`) provides `MemoryRouter` context to page components. CI runs on every PR to `master` via `.github/workflows/frontend_ci.yml` (checkout → Node 22 → npm ci → lint → build → test).

9. **CORS**: By default, the backend allows `http://localhost:5173` (Vite dev server). Configure via `CORS_ORIGINS` env var.

10. **Ports**: Backend runs on `8000`, frontend dev server on `5173`. Ensure both are free.

11. **Redis & RQ Worker**: The word-count feature requires Redis running locally (default: `localhost:6379`). Start the RQ worker in a separate terminal with `python -m backend.worker`. The worker listens on the `qa-jobs` queue. When Redis is unavailable, uploads still succeed but word-count jobs are not enqueued (graceful degradation). The `REDIS_PASSWORD` in `.env.example` (`QaPassword`) is for local dev only — use a secure password in production.
