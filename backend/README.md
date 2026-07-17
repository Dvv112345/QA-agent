# QA Agent Backend

FastAPI + PostgreSQL backend for the QA Agent — manages GitHub repositories and QA sprints. Registering a repo validates it against the GitHub API; creating a sprint downloads the repo's README (or accepts an uploaded one) and captures a filtered file-tree listing, both stored as LLM context. Sprint requirements are analyzed for QA-clarity by an LLM via Redis/RQ background workers, with a clarification question/answer loop per requirement. Once every requirement is confirmed, the user describes test environment access in free text — judged synchronously by the LLM — and confirming it locks the sprint's requirement set. Finally, an LLM generates a test plan per requirement on the same worker infrastructure, reading repository files through a bounded tool loop to ground the test cases; each draft plan goes through a capped feedback loop or uncapped direct edit until approved.

## Quick Start

```bash
# From the repo root
pip install -e ".[dev]"

# Create the database (PostgreSQL must be running)
createdb qa_agent

# Configure environment
cp backend/.env.example backend/.env
# Edit backend/.env for your environment

# Start the server
python -m backend.main
```

The API is served at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`. Tables are created automatically on startup.

### Background worker (requirement analysis + test-plan generation)

Requirement clarity analysis and test-plan generation run on RQ workers backed by Redis (both task types share the same queue and workers). The API works without them — rows just stay `pending` until a worker picks them up (a reconciler in the API process re-enqueues the backlog automatically when Redis recovers).

```bash
# Terminal 2 — start a worker (repo root; reads the same backend/.env)
python -m backend.worker

# Start more workers in additional terminals for concurrency.

# Maintenance: empty the queue and job registries
python -m backend.scripts.clear_queue
```

On Windows the worker automatically uses RQ's `SimpleWorker` (no `os.fork()`). The worker also needs an LLM key: set `OPENAI_API_KEY` (and optionally `OPENAI_BASE_URL` / `OPENAI_MODEL` for any OpenAI-compatible provider; the defaults target DeepSeek). The same key powers the test-environment sufficiency check, which runs synchronously inside the API request — no worker involved.

## Environment Variables

`.env.example` documents every variable. The important ones:

| Variable                                                    | Default                                                  | Description                                                                                                                     |
| ----------------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `DATABASE_URL`                                              | `postgresql://postgres:postgres@localhost:5432/qa_agent` | PostgreSQL connection string.                                                                                                   |
| `ENCRYPTION_KEY`                                            | _(unset)_                                                | Fernet key for encrypting GitHub tokens at rest. Required to register repos with a token.                                       |
| `APP_PASSWORD`                                              | _(unset)_                                                | Shared password for accessing the QA Agent UI. When unset, authentication is disabled.                                          |
| `STORE_OFFLINE`                                             | `false`                                                  | Set to `"true"` to persist sprint README files to disk.                                                                         |
| `STORAGE_LOCATION`                                          | `./uploads`                                              | Directory for sprint files when `STORE_OFFLINE=true`.                                                                           |
| `CORS_ORIGINS`                                              | `http://localhost:5173`                                  | Comma-separated list of allowed origins.                                                                                        |
| `GITHUB_API_TIMEOUT`                                        | `15`                                                     | Timeout in seconds for GitHub API requests.                                                                                     |
| `FILE_TREE_MAX_CHARS`                                       | `20000`                                                  | Character cap for the repo file-tree listing captured at sprint creation.                                                       |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` / `REDIS_DB` | `localhost` / `6379` / _(unset)_ / `0`                   | Redis connection for the analysis queue.                                                                                        |
| `JOB_TIMEOUT` / `JOB_RESULT_TTL` / `WORKER_TTL`             | `300` / `3600` / `30`                                    | RQ job timeout, result retention, and worker heartbeat TTL (bounds Windows shutdown lag).                                       |
| `OPENAI_API_KEY`                                            | _(unset)_                                                | LLM API key. Required for requirement analysis, the test-environment check, and test-plan generation; never logged or returned. |
| `OPENAI_BASE_URL`                                           | `https://api.deepseek.com`                               | Any OpenAI-compatible provider.                                                                                                 |
| `OPENAI_MODEL`                                              | `deepseek-v4-flash`                                      | Model used for the LLM checks.                                                                                                  |
| `OPENAI_TIMEOUT`                                            | `60`                                                     | Timeout in seconds for LLM requests.                                                                                            |
| `MAX_CLARIFICATION_ROUNDS`                                  | `3`                                                      | Clarification Q&A rounds per requirement; past the cap only confirm-as-is or manual edit.                                       |
| `MAX_AUTO_RETRIES`                                          | `3`                                                      | Automatic retries before a requirement is marked `failed`; manual Restart stays uncapped.                                       |
| `MAX_TEST_ENV_REVISION_ROUNDS`                              | `3`                                                      | Answer/revise rounds for the test-environment text; direct edit stays uncapped.                                                 |
| `MAX_TEST_PLAN_FEEDBACK_ROUNDS`                             | `3`                                                      | Feedback/revise rounds per test plan; direct edit stays uncapped.                                                               |
| `TEST_PLAN_TOOL_ROUNDS`                                     | `8`                                                      | Max `read_file` LLM rounds per plan generation before the final answer is forced.                                               |
| `TEST_PLAN_FILE_MAX_CHARS`                                  | `20000`                                                  | Per-file character cap for repo files fetched by the tool loop.                                                                 |
| `TEST_PLAN_JOB_TIMEOUT`                                     | `900`                                                    | RQ job timeout for plan jobs — sized for a worst-case tool loop, unlike `JOB_TIMEOUT`.                                          |
| `RECONCILER_INTERVAL`                                       | `30`                                                     | Seconds between reconciler ticks (re-enqueues lost/backlogged jobs).                                                            |
| `HEARTBEAT_STALE_SECONDS`                                   | `180`                                                    | Age after which an `analyzing` heartbeat counts as a crashed worker; keep above `OPENAI_TIMEOUT`.                               |
| `PENDING_JOB_STALE_SECONDS`                                 | `30`                                                     | Age after which a `pending` row's started RQ job counts as a crashed worker.                                                    |

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

Create a sprint linked to a repo. Refreshes repo metadata from GitHub and captures a filtered, size-capped file-tree listing (best-effort — failures never block creation), then resolves the README: a user-uploaded file wins; otherwise the README is downloaded from GitHub; if the repo has no README and none is uploaded, the request fails. README and file tree become LLM prompt context for later analysis.

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
  "repo": { "id": 1, "name": "owner/repo", "...": "..." },
  "requirements_complete": false,
  "has_test_environment_submission": false,
  "requirements_locked": false,
  "has_test_plans": false,
  "test_plans_complete": false
}
```

The boolean flags are computed by the backend: `requirements_complete` (≥1 requirement and all `confirmed`), `has_test_environment_submission` (a test-environment row exists), `requirements_locked` (the test environment is confirmed, freezing the requirement set), `has_test_plans` (≥1 requirement has a test-plan row), and `test_plans_complete` (every requirement has an `approved` plan).

**Errors:** 404 (repo not found), 422 (empty name, deactivated repo, invalid README, or no README available), 502 (GitHub API failure).

#### `GET /api/sprints`

List sprints — active first, newest first within each group. Supports `offset` / `limit`.

#### `GET /api/sprints/{sprint_id}`

Get a single sprint with its repo info. 404 if not found.

#### `PATCH /api/sprints/{sprint_id}`

Finish a sprint. Body: `{ "active": false }` (the only supported transition). Any `pending`/`analyzing` requirements and `pending`/`generating` test plans are marked `failed` — nothing runs on a finished sprint.

**Errors:** 404 (not found), 422 (already finished or `active` not `false`).

### Requirements

Requirements belong to a sprint and carry a lifecycle status: `pending → analyzing → needs_clarification ⇄ analyzing → ready → confirmed`, plus `failed` (restartable). Analysis happens on the background worker; poll the list endpoint to observe progress.

#### `POST /api/sprints/{sprint_id}/requirements`

Create a batch of requirements (JSON body: `[{ "name": "...", "description": "..." }, …]`). Rows start `pending` and are enqueued for analysis. **Errors:** 404 (sprint), 422 (empty list, blank fields, finished sprint, or requirements locked by a confirmed test environment).

#### `GET /api/sprints/{sprint_id}/requirements`

List a sprint's requirements — the polling endpoint (plain DB read).

#### `POST /api/requirements/{id}/answer`

Answer the clarifying question (`{ "answer": "..." }`); the requirement re-enters analysis, which rewrites the description. Limited to `MAX_CLARIFICATION_ROUNDS` (default 3) per requirement — past the cap this returns 422 and the requirement must be confirmed as-is or edited.

#### `POST /api/requirements/{id}/confirm`

Confirm a `needs_clarification` or `ready` requirement. Confirmed requirements are final: every later mutation except delete returns 422.

#### `PATCH /api/requirements/{id}`

Manually edit the description (`{ "description": "..." }`) from `needs_clarification` or `ready`; re-enters analysis.

#### `POST /api/requirements/{id}/restart`

Restart a `failed` requirement (clears the error; uncapped).

#### `DELETE /api/requirements/{id}`

Remove a requirement (204). Allowed in **every** status, including `confirmed` and mid-analysis — until the sprint's test environment is confirmed (then 422, the requirement set is locked). Also 422 on finished sprints.

### Test Environment

The second sprint stage. Once every requirement is `confirmed` (and at least one exists), the user describes how the test environment is accessed in free text; the LLM judges sufficiency **synchronously inside the request** (offloaded to a thread, bounded by `OPENAI_TIMEOUT`) — no queue or worker involved. One row per sprint. Lifecycle: `needs_info ⇄ ready → confirmed`.

> **Plaintext by design:** the description may contain test credentials. It is stored unencrypted and sent to the LLM provider — prefer vault references over raw secrets.

#### `GET /api/sprints/{sprint_id}/test-environment`

Fetch the sprint's submission (readable on finished sprints). **Errors:** 404 (sprint not found, or no submission yet).

#### `POST /api/sprints/{sprint_id}/test-environment`

Create or update the access description (`{ "content": "..." }`) and run a fresh sufficiency check. Sufficient text → `ready`; insufficient → `needs_info` with a `clarifying_question`. Re-POSTing (direct edit) is always available and uncapped.

**Response** (200): `TestEnvironmentResponse`

```json
{
  "id": 1,
  "sprint_id": 1,
  "content": "Staging at https://staging.example.com, credentials in the team vault…",
  "original_content": "Staging at https://staging.example.com…",
  "status": "ready",
  "clarifying_question": null,
  "revision_count": 0,
  "clarification_cap_reached": false,
  "requirements_stale": false,
  "created_at": "2026-07-13T12:00:00Z",
  "updated_at": "2026-07-13T12:00:00Z"
}
```

**Errors:** 404 (sprint), 422 (finished sprint, requirements not all confirmed, already confirmed, or empty content), 502 (LLM failure — nothing is persisted).

#### `POST /api/test-environment/{te_id}/answer`

Answer the clarifying question (`{ "answer": "..." }`); the LLM rewrites the description and re-judges it. Capped at `MAX_TEST_ENV_REVISION_ROUNDS` (default 3) — past the cap this returns 422 and the text must be edited directly (re-POST, uncapped).

**Errors:** 404, 422 (not `needs_info`, cap reached, empty answer, finished sprint, requirements incomplete), 502 (LLM failure).

#### `POST /api/test-environment/{te_id}/confirm`

Finalize the access description. Terminal — and it **locks the sprint's requirement set** (requirement create/delete return 422 afterwards).

**Errors:** 404, 422 (not `ready`, finished sprint, requirements incomplete, or `requirements_stale` — a confirmed requirement changed since the last check; re-POST the current content to re-check first).

### Test Plans

The third sprint stage, available once the test environment is confirmed (`requirements_locked`). One plan per confirmed requirement, generated asynchronously on the RQ worker: the LLM may call a `read_file(path)` tool up to `TEST_PLAN_TOOL_ROUNDS` times (paths validated against the captured file tree, contents truncated to `TEST_PLAN_FILE_MAX_CHARS`) before returning a structured plan — complexity (`low`/`medium`/`high`), summary, and ≥1 test case (title, optional preconditions, newline-joined steps, expected result, type, priority). Lifecycle: `pending → generating → draft ⇄ generating (feedback revision) → approved` (terminal), plus `failed` (restartable). Poll the list endpoint to observe progress.

#### `POST /api/sprints/{sprint_id}/test-plans/generate`

Create a `pending` plan for every confirmed requirement and enqueue one generation job each. Idempotent — requirements that already have a plan are skipped, `failed` plans are reset like Restart (keeping any interrupted feedback), and the sprint's full plan list is returned either way.

**Response** (200): `list[TestPlanResponse]`

```json
[
  {
    "id": 1,
    "requirement_id": 1,
    "requirement_name": "Login",
    "requirement_description": "Registered users can log in…",
    "status": "pending",
    "complexity": null,
    "summary": null,
    "revision_count": 0,
    "feedback_cap_reached": false,
    "error": null,
    "cases": [],
    "created_at": "2026-07-17T12:00:00Z",
    "updated_at": "2026-07-17T12:00:00Z"
  }
]
```

Once drafted, `cases` holds objects with `id`, `position`, `title`, `preconditions`, `steps` (newline-joined), `expected_result`, `case_type`, and `priority`.

**Errors:** 404 (sprint), 422 (finished sprint, or test environment not confirmed).

#### `GET /api/sprints/{sprint_id}/test-plans`

List a sprint's plans, ordered by requirement creation — the polling endpoint (plain DB read; readable on finished sprints). 404 on unknown sprint.

#### `POST /api/test-plans/{plan_id}/feedback`

Send free-text feedback on a `draft` plan (`{ "feedback": "..." }`); the plan re-enters generation and the LLM produces a full revised plan. Capped at `MAX_TEST_PLAN_FEEDBACK_ROUNDS` (default 3) per plan — past the cap this returns 422 and the plan must be edited directly (uncapped).

**Errors:** 404, 422 (not `draft`, cap reached, empty feedback, finished sprint).

#### `PATCH /api/test-plans/{plan_id}`

Directly edit a `draft` plan — no LLM involved, uncapped, never increments `revision_count`, stays `draft`. Body: `{ "complexity": "low|medium|high", "summary": "...", "cases": [{ "title": "...", "preconditions": null, "steps": "one step per line", "expected_result": "...", "case_type": "...", "priority": "high|medium|low" }, …] }`. Cases are replaced wholesale.

**Errors:** 404, 422 (not `draft`, finished sprint, or field validation: no cases, blank title/steps/expected result/type, invalid priority/complexity).

#### `POST /api/test-plans/{plan_id}/approve`

Approve a `draft` plan. Terminal — no unapprove, no regenerate; feedback/edit return 422 afterwards. When every requirement's plan is approved, `SprintResponse.test_plans_complete` flips to `true`.

**Errors:** 404, 422 (not `draft`, finished sprint).

#### `POST /api/test-plans/{plan_id}/restart`

Restart a `failed` plan (clears the error and retry counter; keeps pending feedback so an interrupted revision resumes; uncapped).

**Errors:** 404, 422 (not `failed`, finished sprint).

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
  models/
    database.py        # Table models: Repo, Sprint, Requirement, TestEnvironmentAccess, TestPlan, TestCase
    types.py           # Request/response types
  routes/
    auth.py            # POST /api/auth/verify, GET /api/auth/check
    repos.py           # Repo registration, listing, deactivation, README status
    sprints.py         # Sprint create/list/get/finish
    requirements.py    # Requirement CRUD + clarification/confirm/restart
    test_environment.py # Test environment get/submit/answer/confirm (synchronous LLM check)
    test_plans.py      # Test plan generate/list/feedback/edit/approve/restart
  services/
    storage.py         # Conditional README persistence (STORE_OFFLINE)
    queue.py           # RQ queue service (graceful degradation when Redis is down)
    llm.py             # OpenAI-SDK client: clarity/test-env checks + test-plan tool loop
    reconciler.py      # Re-enqueues lost jobs, sweeps crashed-worker heartbeats (requirements + plans)
  tasks/
    analyze_requirement.py  # The analysis task executed by the worker
    generate_test_plan.py   # The plan-generation task (bounded read_file tool loop)
  scripts/
    clear_queue.py     # Queue maintenance CLI (python -m backend.scripts.clear_queue)
    reset_db.py        # Drop + recreate all tables (python -m backend.scripts.reset_db)
  utils/
    auth.py            # verify_auth cookie dependency
    crypto.py          # Fernet encryption for GitHub tokens
    github_utils.py    # GitHub API client and error hierarchy
    readme_utils.py    # Best-effort README resolution (stored copy → re-download → none)
    sprint_utils.py    # Unique sprint directory generation
  tests/               # pytest suite (in-memory SQLite, mocked GitHub API, Redis + LLM stubbed)
```

## Requirements

- Python 3.10+
- PostgreSQL
- Redis (for requirement analysis and test-plan generation; optional otherwise)
- An LLM API key (`OPENAI_API_KEY` — for requirement analysis, the test-environment check, and test-plan generation)
- Dependencies declared in `pyproject.toml` (install with `pip install -e ".[dev]"` from the repo root)
