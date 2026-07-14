# QA Agent Backend

FastAPI + PostgreSQL backend for the QA Agent — manages GitHub repositories and QA sprints. Registering a repo validates it against the GitHub API; creating a sprint downloads the repo's README (or accepts an uploaded one) and stores it for later analysis. Sprint requirements are analyzed for QA-clarity by an LLM via Redis/RQ background workers.

## Quick Start

```bash
# From the repo root
pip install -r backend/requirements.txt

# Create the database (PostgreSQL must be running)
createdb qa_agent

# Configure environment
cp backend/.env.example backend/.env
# Edit backend/.env for your environment

# Start the server
python -m backend.main
```

The API is served at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`. Tables are created automatically on startup.

### Background worker (requirement analysis)

Requirement clarity analysis runs on RQ workers backed by Redis. The API works without them — requirements just stay `pending` until a worker picks them up (a reconciler in the API process re-enqueues the backlog automatically when Redis recovers).

```bash
# Terminal 2 — start a worker (repo root; reads the same backend/.env)
python -m backend.worker

# Start more workers in additional terminals for concurrency.

# Maintenance: empty the queue and job registries
python -m backend.clear_queue
```

On Windows the worker automatically uses RQ's `SimpleWorker` (no `os.fork()`). Analysis also needs an LLM key: set `OPENAI_API_KEY` (and optionally `OPENAI_BASE_URL` / `OPENAI_MODEL` for any OpenAI-compatible provider; the defaults target DeepSeek).

## Environment Variables

`.env.example` documents every variable. The important ones:

| Variable                                       | Default                                                  | Description                                                                               |
| ---------------------------------------------- | -------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `DATABASE_URL`                                 | `postgresql://postgres:postgres@localhost:5432/qa_agent` | PostgreSQL connection string.                                                             |
| `ENCRYPTION_KEY`                               | _(unset)_                                                | Fernet key for encrypting GitHub tokens at rest. Required to register repos with a token. |
| `APP_PASSWORD`                                 | _(unset)_                                                | Shared password for accessing the QA Agent UI. When unset, authentication is disabled.    |
| `STORE_OFFLINE`                                | `false`                                                  | Set to `"true"` to persist sprint README files to disk.                                   |
| `STORAGE_LOCATION`                             | `./uploads`                                              | Directory for sprint files when `STORE_OFFLINE=true`.                                     |
| `CORS_ORIGINS`                                 | `http://localhost:5173`                                  | Comma-separated list of allowed origins.                                                  |
| `GITHUB_API_TIMEOUT`                           | `15`                                                     | Timeout in seconds for GitHub API requests.                                               |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | `localhost` / `6379` / _(unset)_                         | Redis connection for the analysis queue.                                                  |
| `OPENAI_API_KEY`                               | _(unset)_                                                | LLM API key. Required for requirement analysis; never logged or returned.                 |
| `OPENAI_BASE_URL`                              | `https://api.deepseek.com`                               | Any OpenAI-compatible provider; empty = api.openai.com.                                   |
| `OPENAI_MODEL`                                 | `deepseek-v4-flash`                                      | Model used for requirement clarity analysis.                                              |

Generate an encryption key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## API Endpoints

All routes except `/api/health` and `/api/auth/*` require authentication when `APP_PASSWORD` is set (see [Authentication](#authentication)).

### Health

#### `GET /api/health`

```json
{ "status": "ok", "storage": "memory_only", "redis": "available" }
```

`storage` is `"memory_only"`, `"available"`, or `"unavailable: <reason>"`. `redis` is `"available"` or `"unavailable: <reason>"`.

### Repos

#### `POST /api/repos`

Register a GitHub repository. Validates the URL format and confirms the repo is accessible via the GitHub API.

**Request:** `multipart/form-data`

| Field          | Required | Description                                        |
| -------------- | -------- | -------------------------------------------------- |
| `github_url`   | Yes      | e.g. `https://github.com/owner/repo`               |
| `access_token` | No       | GitHub token for private repos (encrypted at rest) |

**Response** (201): `RepoResponse`

```json
{
  "id": 1,
  "github_link": "https://github.com/owner/repo",
  "name": "owner/repo",
  "description": "A description from GitHub",
  "active": true,
  "created_at": "2026-07-13T12:00:00Z"
}
```

The access token is never returned in responses.

**Errors:** 422 (invalid URL or inaccessible repo), 500 (missing `ENCRYPTION_KEY` when a token is supplied).

#### `GET /api/repos`

List active repos, newest first. Supports `offset` / `limit` query params.

#### `POST /api/repos/{repo_id}/deactivate`

Soft-delete a repo (sets `active=false`, clears the stored token). Returns `{ "deactivated": true }`.

**Errors:** 404 (not found), 422 (already deactivated, or active sprints still reference it).

#### `GET /api/repos/{repo_id}/readme-status`

Check whether the GitHub repo has a README. Returns `{ "has_readme": true }`.

**Errors:** 404 (not found), 502 (GitHub API failure).

### Sprints

#### `POST /api/sprints`

Create a sprint linked to a repo. Refreshes repo metadata from GitHub, then resolves the README: a user-uploaded file wins; otherwise the README is downloaded from GitHub; if the repo has no README and none is uploaded, the request fails.

**Request:** `multipart/form-data`

| Field         | Required | Description                                        |
| ------------- | -------- | -------------------------------------------------- |
| `name`        | Yes      | Sprint name                                        |
| `repo_id`     | Yes      | ID of an active registered repo                    |
| `readme_file` | No       | `.md` / `.markdown` file (UTF-8), overrides GitHub |

**Response** (201): `SprintResponse`

```json
{
  "id": 1,
  "name": "Sprint 1",
  "repo_id": 1,
  "active": true,
  "directory": "0f8a2b…",
  "created_at": "2026-07-13T12:00:00Z",
  "repo": { "id": 1, "name": "owner/repo", "...": "..." }
}
```

**Errors:** 404 (repo not found), 422 (empty name, deactivated repo, invalid README, or no README available), 502 (GitHub API failure).

#### `GET /api/sprints`

List sprints — active first, newest first within each group. Supports `offset` / `limit`.

#### `GET /api/sprints/{sprint_id}`

Get a single sprint with its repo info. 404 if not found.

#### `PATCH /api/sprints/{sprint_id}`

Finish a sprint. Body: `{ "active": false }` (the only supported transition).

**Errors:** 404 (not found), 422 (already finished or `active` not `false`).

### Requirements

Requirements belong to a sprint and carry a lifecycle status: `pending → analyzing → needs_clarification ⇄ analyzing → ready → confirmed`, plus `failed` (restartable). Analysis happens on the background worker; poll the list endpoint to observe progress.

#### `POST /api/sprints/{sprint_id}/requirements`

Create a batch of requirements (JSON body: `[{ "name": "...", "description": "..." }, …]`). Rows start `pending` and are enqueued for analysis. **Errors:** 404 (sprint), 422 (empty list, blank fields, finished sprint).

#### `GET /api/sprints/{sprint_id}/requirements`

List a sprint's requirements — the polling endpoint (plain DB read).

#### `POST /api/requirements/{id}/answer`

Answer the clarifying question (`{ "answer": "..." }`); the requirement re-enters analysis, which rewrites the description. Limited to 3 rounds per requirement — past the cap this returns 422 and the requirement must be confirmed as-is or edited.

#### `POST /api/requirements/{id}/confirm`

Confirm a `needs_clarification` or `ready` requirement. Confirmed requirements are final: every later mutation except delete returns 422.

#### `PATCH /api/requirements/{id}`

Manually edit the description (`{ "description": "..." }`) from `needs_clarification` or `ready`; re-enters analysis.

#### `POST /api/requirements/{id}/restart`

Restart a `failed` requirement (clears the error; uncapped).

#### `DELETE /api/requirements/{id}`

Remove a requirement (204). Allowed in **every** status, including `confirmed` and mid-analysis. 422 on finished sprints.

### Authentication

#### `POST /api/auth/verify`

Submit a password. Sets an HttpOnly `qa_auth` session cookie on match.

**Request:** `application/json` — `{ "password": "my-password" }`

**Response** (200): `{ "valid": true }` or `{ "valid": false }`. If `APP_PASSWORD` is unset, always returns `{ "valid": true }` without setting a cookie.

#### `GET /api/auth/check`

Report whether the current `qa_auth` cookie is valid. Never returns 401.

**Response** (200): `{ "valid": true }` or `{ "valid": false }`.

### Example with curl

```bash
# Verify password and store the cookie
curl -X POST http://localhost:8000/api/auth/verify \
  -H "Content-Type: application/json" \
  -d '{"password": "my-password"}' \
  -c cookies.txt

# Register a repo with the stored cookie
curl -X POST http://localhost:8000/api/repos \
  -F "github_url=https://github.com/owner/repo" \
  -b cookies.txt

# Create a sprint
curl -X POST http://localhost:8000/api/sprints \
  -F "name=Sprint 1" \
  -F "repo_id=1" \
  -b cookies.txt
```

## Project Structure

```
backend/
  main.py              # App factory, CORS, exception handlers, health check, reconciler lifespan
  config.py            # Environment variable configuration
  database.py          # Engine, session dependency, table initialisation
  worker.py            # RQ worker CLI (python -m backend.worker)
  clear_queue.py       # Queue maintenance CLI
  models/
    database.py        # Table models: Repo, Sprint, Requirement
    types.py           # Request/response types
  routes/
    auth.py            # POST /api/auth/verify, GET /api/auth/check
    repos.py           # Repo registration, listing, deactivation, README status
    sprints.py         # Sprint create/list/get/finish
    requirements.py    # Requirement CRUD + clarification/confirm/restart
  services/
    storage.py         # Conditional README persistence (STORE_OFFLINE)
    queue.py           # RQ queue service (graceful degradation when Redis is down)
    llm.py             # OpenAI-SDK client for clarity checks and revisions
    reconciler.py      # Re-enqueues lost jobs, sweeps crashed-worker heartbeats
  tasks/
    analyze_requirement.py  # The analysis task executed by the worker
  utils/
    auth.py            # verify_auth cookie dependency
    crypto.py          # Fernet encryption for GitHub tokens
    github_utils.py    # GitHub API client and error hierarchy
    sprint_utils.py    # Unique sprint directory generation
  tests/               # pytest suite (in-memory SQLite, mocked GitHub API, Redis + LLM stubbed)
```

## Requirements

- Python 3.10+
- PostgreSQL
- Redis (for requirement analysis; optional otherwise)
- Dependencies listed in `requirements.txt`
