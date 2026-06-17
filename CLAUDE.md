# CLAUDE.md

## Project Overview

QA Agent is a full-stack application that accepts source code (zip archives) and requirement documents (markdown) for automated Quality Assurance analysis. It consists of a FastAPI backend and a React + Vite frontend.

## Tech Stack

| Layer     | Technology                                                        |
| --------- | ----------------------------------------------------------------- |
| Backend   | Python 3.10+, FastAPI, Uvicorn, SQLModel, python-dotenv           |
| Frontend  | React 19, TypeScript 6, Vite 8, ESLint 10, Prettier 3             |
| Dev tools | pre-commit (Ruff, Prettier, ESLint, general hooks), pytest, httpx |

## Build and Test Commands

### Backend (run from repo root)

```bash
# Install dependencies
pip install -r backend/requirements.txt

# Start the API server (http://localhost:8000)
python -m backend.main

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
├── backend/
│   ├── README.md                       # Backend-specific docs: quick start, env vars, API endpoints
│   ├── requirements.txt                # Python dependencies: fastapi, uvicorn, python-multipart, sqlmodel, dotenv
│   ├── .env.example                    # Template for environment variables with documentation
│   ├── .env                            # Actual environment variables (git-ignored)
│   ├── main.py                         # FastAPI app factory, CORS, exception handlers, health endpoint, CLI entry point
│   ├── config.py                       # Typed env-var helpers (_get_bool, _get_int, _get_list, _get_optional_path)
│   ├── tests/
│   │   ├── __init__.py
│   │   ├── conftest.py                  # Fixtures: async_client, sample_zip_bytes, sample_md_bytes
│   │   ├── sample_zip.zip               # Sample zip for upload tests
│   │   ├── sample_md.md                 # Sample markdown for upload tests
│   │   ├── test_config.py               # Config module tests
│   │   ├── test_main.py                 # create_app, health, CORS, exception handler tests
│   │   ├── test_routes.py               # Upload route tests
│   │   ├── test_storage.py              # StorageService tests
│   │   └── test_zip_utils.py            # Zip extraction and path traversal tests
│   ├── models/
│   │   ├── __init__.py
│   │   └── types.py                    # SQLModel types: HealthResponse, UploadResponse
│   ├── routes/
│   │   ├── __init__.py
│   │   └── upload.py                   # POST /api/upload — accepts zip + markdown, validates, extracts, returns tree
│   ├── services/
│   │   ├── __init__.py
│   │   └── storage.py                  # StorageService — conditional disk persistence when STORE_OFFLINE=true
│   └── utils/
│       ├── __init__.py
│       └── zip_utils.py                # Safe zip extraction (path traversal protection, streaming I/O, tree rendering)
├── frontend/
│   ├── README.md                       # Vite + React template README
│   ├── .gitignore                      # Frontend-specific ignores: node_modules, dist, .vscode, .idea
│   ├── package.json                    # Dependencies and scripts
│   ├── .prettierrc                     # Prettier config: no semis, single quotes, trailing commas, 100 print width
│   ├── eslint.config.js                # ESLint flat config: JS recommended, TS recommended, React hooks, React refresh
│   ├── index.html                      # HTML entry point with <div id="root">
│   ├── vite.config.ts                  # Vite config with @vitejs/plugin-react
│   ├── tsconfig.json                   # Root TS config — references tsconfig.app.json and tsconfig.node.json
│   ├── tsconfig.app.json               # App TS config: ES2023, bundler mode, react-jsx, strict linting
│   ├── tsconfig.node.json              # Node TS config: ES2023, bundler mode (for vite.config.ts)
│   ├── public/
│   │   ├── favicon.svg                 # Site favicon
│   │   └── icons.svg                   # SVG sprite sheet for icons (documentation, social, GitHub, Discord, X, Bluesky)
│   └── src/
│       ├── main.tsx                    # React entry point — renders <App /> into #root with StrictMode
│       ├── App.tsx                     # Main app component — hero section, counter demo, docs/social links
│       ├── App.css                     # App-level styles
│       ├── index.css                   # Global reset/base styles
│       └── assets/
│           ├── hero.png                # Hero section background image
│           ├── react.svg               # React logo
│           └── vite.svg                # Vite logo
```

## Code Patterns

### Configuration

All backend config comes from environment variables, loaded via `python-dotenv` in `backend/config.py`. Each variable has a typed getter (`_get_bool`, `_get_int`, `_get_list`, `_get_optional_path`) with sensible defaults. Copy `.env.example` to `.env` to customize.

### API Patterns

- **FastAPI app factory**: `create_app()` in `main.py` wires up middleware, routers, exception handlers, and health checks.
- **Exception handling**: A global `Exception` handler catches unexpected errors, re-raises `HTTPException` for FastAPI to handle normally, and returns 500 for everything else.
- **Validation**: Route handlers validate file extensions, content magic bytes, size limits, and encoding before processing.
- **Response types**: All responses use `SQLModel` types defined in `models/types.py`.

### Frontend Patterns

- **Strict TypeScript**: `noUnusedLocals`, `noUnusedParameters`, `erasableSyntaxOnly`, `noFallthroughCasesInSwitch` are all enabled.
- **Flat ESLint config**: Uses the new ESLint flat config format (`eslint/config`'s `defineConfig`).
- **Vite with Oxc**: Uses `@vitejs/plugin-react` which leverages Oxc for fast transforms.
- **StrictMode**: React StrictMode is enabled in `main.tsx`.

### Pre-commit Quality Gates

All staged files are auto-formatted and linted before commit:

- **Python**: Ruff lint (with auto-fix) + Ruff format
- **Frontend**: Prettier + ESLint (with auto-fix)
- **General**: trailing whitespace removal, EOF newlines, merge conflict detection, large file check (500KB), private key detection, YAML/TOML validation

### File Upload Flow

1. Client POSTs zip + markdown to `/api/upload`
2. Route validates extensions (`.zip`, `.md`/`.markdown`)
3. Reads files into memory, validates magic bytes (ZIP header) and UTF-8 encoding
4. Generates a job ID (`YYYYMMDD-HHMMSS-<6 hex chars>`)
5. If `STORE_OFFLINE=true`, persists to `STORAGE_LOCATION/<job_id>/`
6. Extracts zip to temp directory (or reads from stored path), builds directory tree
7. Returns `UploadResponse` with job_id, status, filenames, tree (list + text)

## CLI Commands

| Command                                   | Purpose                                       |
| ----------------------------------------- | --------------------------------------------- |
| `python -m backend.main`                  | Start the FastAPI dev server on port 8000     |
| `pip install -r backend/requirements.txt` | Install/update Python runtime dependencies    |
| `pip install -e ".[dev]"`                 | Install Python dev dependencies (pytest, etc) |
| `python -m pytest -v`                     | Run all backend tests                         |
| `python -m pytest -k <keyword> -v`        | Run tests matching a keyword                  |
| `cd frontend && npm install`              | Install/update frontend dependencies          |
| `cd frontend && npm run dev`              | Start Vite dev server with HMR on port 5173   |
| `cd frontend && npm run build`            | Type-check and build frontend for production  |
| `cd frontend && npm run lint`             | Lint frontend with ESLint                     |
| `cd frontend && npm run preview`          | Preview production build locally              |
| `pre-commit install`                      | Install git pre-commit hooks                  |
| `pre-commit run --all-files`              | Run all pre-commit hooks on all files         |
| `pre-commit run <hook-id> --all-files`    | Run a specific hook on all files              |

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

8. **Tests**: Backend tests live in `backend/tests/` and run with `pytest` via `python -m pytest -v`. Tests are colocated with the backend package. Dev dependencies (pytest, pytest-asyncio, httpx) are declared in `pyproject.toml` under `[project.optional-dependencies] dev`. Install with `pip install -e ".[dev]"`. CI runs on every PR to `master` via `.github/workflows/ci.yml`. Frontend tests don't exist yet — when added, colocate them with the component they test (e.g., `Button.test.tsx` next to `Button.tsx`).

9. **CORS**: By default, the backend allows `http://localhost:5173` (Vite dev server). Configure via `CORS_ORIGINS` env var.

10. **Ports**: Backend runs on `8000`, frontend dev server on `5173`. Ensure both are free.
