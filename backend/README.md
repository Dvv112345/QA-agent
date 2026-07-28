# QA Agent Backend

FastAPI + PostgreSQL backend for the QA Agent — manages GitHub repositories and QA sprints. Registering a repo validates it against the GitHub API; creating a sprint downloads the repo's README (or accepts an uploaded one) and captures a filtered file-tree listing, both stored as LLM context. Sprint requirements are entered manually or extracted from an uploaded PRD document (split into requirements by a synchronous LLM call), then analyzed for QA-clarity by an LLM via Redis/RQ background workers, with a clarification question/answer loop per requirement. Once every requirement is confirmed, the user describes test environment access in free text — judged synchronously by the LLM, which also extracts the access details into structured, editable environment variables — and confirming it locks the sprint's requirement set. Next, an LLM generates a test plan per requirement on the same worker infrastructure, reading repository files through a bounded tool loop to ground the test cases; each draft plan goes through a capped feedback loop or uncapped direct edit until approved. Finally, running the approved plans generates (or reuses) a Playwright script per test case, executes it in a subprocess against the confirmed environment, and self-heals script bugs via an LLM diagnosis loop — stopping and reporting a `failed` case as soon as a failure looks like a genuine application bug.

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

### Background worker (requirement analysis + test-plan generation + test execution + exploratory testing)

Requirement clarity analysis, test-plan generation, test execution, and exploratory testing run on RQ workers backed by Redis (all four task types share the same queue and workers). The API works without them — rows just stay `pending` until a worker picks them up (a reconciler in the API process re-enqueues the backlog automatically when Redis recovers).

```bash
# Terminal 2 — start a worker (repo root; reads the same backend/.env)
python -m backend.worker

# Start more workers in additional terminals for concurrency.

# Maintenance: empty the queue and job registries
python -m backend.scripts.clear_queue
```

On Windows the worker automatically uses RQ's `SimpleWorker` (no `os.fork()`). The worker also needs an LLM key: set `OPENAI_API_KEY` (and optionally `OPENAI_BASE_URL` / `OPENAI_MODEL` for any OpenAI-compatible provider; the defaults target DeepSeek). The same key powers the test-environment sufficiency check, which runs synchronously inside the API request — no worker involved. Test execution and exploratory testing additionally need the Playwright browser binary on the worker host — run `playwright install chromium` once (the `playwright` pip package alone doesn't include it). Exploratory testing drives that browser directly from the worker process (not a subprocess), so it must run headless unless you set `EXPLORATORY_HEADLESS=false` to watch it. If you use conda, always start the worker (`python -m backend.worker`) from an **activated** environment — generated test scripts run via `subprocess.run([sys.executable, ...])`, which guarantees the same interpreter/site-packages regardless of activation state, but inherits the worker's own `os.environ` unchanged, so PATH-dependent behavior is only as correct as however the worker process itself was started.

Generated test scripts may import Playwright, `requests`, `Faker`, `psycopg2` (PostgreSQL), `sqlite3`, and the Python standard library — this set is advertised to the LLM in the script-generation and diagnosis prompts (`llm_prompts.AVAILABLE_TEST_LIBRARIES`) so it doesn't guess at unavailable packages.

## Environment Variables

`.env.example` documents every variable. The important ones:

| Variable                                                    | Default                                                  | Description                                                                                                                     |
| ----------------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `DATABASE_URL`                                              | `postgresql://postgres:postgres@localhost:5432/qa_agent` | PostgreSQL connection string.                                                                                                   |
| `ENCRYPTION_KEY`                                            | _(unset)_                                                | Fernet key for encrypting GitHub tokens at rest. Required to register repos with a token.                                       |
| `APP_PASSWORD`                                              | _(unset)_                                                | Shared password for accessing the QA Agent UI. When unset, authentication is disabled.                                          |
| `STORE_OFFLINE`                                             | `false`                                                  | Set to `"true"` to persist sprint README and uploaded PRD files to disk.                                                        |
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
| `PRD_MAX_CHARS`                                             | `50000`                                                  | Character cap on text extracted from an uploaded PRD; larger uploads are rejected (422), never truncated.                       |
| `MAX_PRD_REQUIREMENTS`                                      | `50`                                                     | Max requirements a single PRD split may produce; larger splits are rejected (422).                                              |
| `MAX_TEST_ENV_REVISION_ROUNDS`                              | `3`                                                      | Answer/revise rounds for the test-environment text; direct edit stays uncapped.                                                 |
| `MAX_TEST_PLAN_FEEDBACK_ROUNDS`                             | `3`                                                      | Feedback/revise rounds per test plan; direct edit stays uncapped.                                                               |
| `TEST_PLAN_TOOL_ROUNDS`                                     | `2`                                                      | Max `read_file` LLM rounds per plan generation before the final answer is forced.                                               |
| `TEST_PLAN_FILE_MAX_CHARS`                                  | `20000`                                                  | Per-file character cap for repo files fetched by the tool loop.                                                                 |
| `TEST_PLAN_JOB_TIMEOUT`                                     | `900`                                                    | RQ job timeout for plan jobs — sized for a worst-case tool loop, unlike `JOB_TIMEOUT`.                                          |
| `MAX_SCRIPT_FIX_ROUNDS`                                     | `3`                                                      | Additional self-heal attempts per test case before a stubborn `script_bug` verdict gives up (case ends `error`, not `failed`).  |
| `TEST_EXECUTION_TOOL_ROUNDS`                                | `5`                                                      | Max `read_file` LLM rounds per test-script generation/diagnosis call.                                                           |
| `SCRIPT_EXECUTION_TIMEOUT`                                  | `60`                                                     | Wall-clock timeout in seconds for one test-script subprocess run.                                                               |
| `TEST_EXECUTION_JOB_TIMEOUT`                                | `3600`                                                   | RQ job timeout for test-execution jobs — sized for every case in a plan, each with multiple generate/execute/diagnose cycles.   |
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

Create a sprint linked to a repo. Refreshes repo metadata from GitHub and captures a filtered, size-capped file-tree listing (best-effort — failures never block creation), then resolves the README: a user-uploaded file wins; otherwise the README is downloaded from GitHub; if the repo has no README and none is uploaded, the request fails. README and file tree become LLM prompt context for later analysis. Whether the README came from an upload is recorded on `Sprint.readme_user_provided` — later stages (test execution) use it to decide whether a README refresh from GitHub is safe (see [Test Execution](#test-execution)).

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
  "test_plans_complete": false,
  "has_test_runs": false
}
```

The boolean flags are computed by the backend: `requirements_complete` (≥1 requirement and all `confirmed`), `has_test_environment_submission` (a test-environment row exists), `requirements_locked` (the test environment is confirmed, freezing the requirement set), `has_test_plans` (≥1 requirement has a test-plan row), `test_plans_complete` (every requirement has an `approved` plan), and `has_test_runs` (≥1 test run has been submitted).

**Errors:** 404 (repo not found), 422 (empty name, deactivated repo, invalid README, or no README available), 502 (GitHub API failure).

#### `GET /api/sprints`

List sprints — active first, newest first within each group. Supports `offset` / `limit`.

#### `GET /api/sprints/{sprint_id}`

Get a single sprint with its repo info. 404 if not found.

#### `PATCH /api/sprints/{sprint_id}`

Finish a sprint. Body: `{ "active": false }` (the only supported transition). Any `pending`/`analyzing` requirements, `pending`/`generating` test plans, `pending`/`running` test executions, and `pending`/`running` exploratory runs are marked `failed` — nothing runs on a finished sprint.

**Errors:** 404 (not found), 422 (already finished or `active` not `false`).

### Requirements

Requirements belong to a sprint and carry a lifecycle status: `pending → analyzing → needs_clarification ⇄ analyzing → ready → confirmed`, plus `failed` (restartable). Analysis happens on the background worker; poll the list endpoint to observe progress.

#### `POST /api/sprints/{sprint_id}/requirements`

Create a batch of requirements (JSON body: `[{ "name": "...", "description": "..." }, …]`). Rows start `pending` and are enqueued for analysis. **Errors:** 404 (sprint), 422 (empty list, blank fields, finished sprint, or requirements locked by a confirmed test environment).

#### `POST /api/sprints/{sprint_id}/requirements/from-prd`

Upload a PRD document and have an LLM split it into requirements — the alternative to entering them manually. The split runs **synchronously inside the request** (offloaded to a thread, bounded by `OPENAI_TIMEOUT`); the resulting rows start `pending` with `from_prd: true` and enter the normal analysis pipeline.

**Request:** `multipart/form-data`

| Field      | Required | Description                                                           |
| ---------- | -------- | --------------------------------------------------------------------- |
| `prd_file` | Yes      | `.md` / `.markdown` / `.txt` (UTF-8), `.pdf`, or `.docx` PRD document |

**Response** (201): `list[RequirementResponse]` — the newly created rows.

Re-uploading a PRD **replaces** the previous upload's `from_prd` rows (in the same transaction as the new inserts); manually entered requirements are never touched. Every failure — invalid file, unreadable/empty document, text over `PRD_MAX_CHARS`, zero or more than `MAX_PRD_REQUIREMENTS` extracted requirements, LLM failure — happens before that transaction, so a failed upload never destroys existing requirements. When `STORE_OFFLINE=true` the original file is saved to the sprint directory as `PRD<ext>` (best-effort).

**Errors:** 404 (sprint), 422 (finished sprint, requirements locked, unsupported/corrupt/empty/oversized file, no requirements found, too many requirements), 502 (LLM failure — nothing persisted).

#### `GET /api/sprints/{sprint_id}/requirements`

List a sprint's requirements — the polling endpoint (plain DB read).

#### `POST /api/requirements/{id}/answer`

Answer the clarifying question (`{ "answer": "..." }`); the requirement re-enters analysis, which rewrites the description. Limited to `MAX_CLARIFICATION_ROUNDS` (default 3) per requirement — past the cap this returns 422 and the requirement must be confirmed as-is or edited.

#### `POST /api/requirements/{id}/confirm`

Confirm a `needs_clarification` or `ready` requirement. Confirmed requirements are final: every later mutation except delete returns 422.

#### `POST /api/sprints/{sprint_id}/requirements/confirm-all`

Confirm every currently `needs_clarification`/`ready` requirement in the sprint in one request (a single set-based `UPDATE`). Ineligible rows (`pending`, `analyzing`, already `confirmed`, `failed`) are left untouched. **Response** (200): `list[RequirementResponse]` — the sprint's full requirement list. **Errors:** 404 (sprint), 422 (finished sprint).

#### `PATCH /api/requirements/{id}`

Manually edit the description (`{ "description": "..." }`) from `needs_clarification` or `ready`; re-enters analysis.

#### `POST /api/requirements/{id}/restart`

Restart a `failed` requirement (clears the error; uncapped).

#### `DELETE /api/requirements/{id}`

Remove a requirement (204). Allowed in **every** status, including `confirmed` and mid-analysis — until the sprint's test environment is confirmed (then 422, the requirement set is locked). Also 422 on finished sprints.

### Test Environment

The second sprint stage. Once every requirement is `confirmed` (and at least one exists), the user describes how the test environment is accessed in free text; the LLM judges sufficiency **synchronously inside the request** (offloaded to a thread, bounded by `OPENAI_TIMEOUT`) — no queue or worker involved. One row per sprint. Lifecycle: `needs_info ⇄ ready → confirmed`.

Whenever a check comes back sufficient, a second synchronous LLM call extracts the access details (URL, credentials, …) into a structured `{"NAME": "value"}` map (`env_vars` in the response) — cleared back to `null` if a later resubmission comes back insufficient. The extracted variables are directly editable (uncapped, no LLM call) any time before confirming.

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
  "env_vars": { "BASE_URL": "https://staging.example.com" },
  "created_at": "2026-07-13T12:00:00Z",
  "updated_at": "2026-07-13T12:00:00Z"
}
```

**Errors:** 404 (sprint), 422 (finished sprint, requirements not all confirmed, already confirmed, or empty content), 502 (LLM failure — nothing is persisted).

#### `POST /api/test-environment/{te_id}/answer`

Answer the clarifying question (`{ "answer": "..." }`); the LLM rewrites the description and re-judges it. Capped at `MAX_TEST_ENV_REVISION_ROUNDS` (default 3) — past the cap this returns 422 and the text must be edited directly (re-POST, uncapped).

**Errors:** 404, 422 (not `needs_info`, cap reached, empty answer, finished sprint, requirements incomplete), 502 (LLM failure).

#### `PATCH /api/test-environment/{te_id}/env-vars`

Directly correct the LLM-extracted variables — no LLM call, uncapped, doesn't touch `content`/`status`/`revision_count`. Body: `{ "variables": { "NAME": "value", … } }`.

**Errors:** 404, 422 (finished sprint, already confirmed, empty `variables`, or a blank name/value).

#### `POST /api/test-environment/{te_id}/confirm`

Finalize the access description. Terminal — and it **locks the sprint's requirement set** (requirement create/delete return 422 afterwards).

**Errors:** 404, 422 (not `ready`, finished sprint, requirements incomplete, `requirements_stale` — a confirmed requirement changed since the last check; re-POST the current content to re-check first — or environment variables not yet extracted, which should be unreachable).

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

#### `POST /api/sprints/{sprint_id}/test-plans/approve-all`

Approve every currently `draft` plan in the sprint in one request (a single set-based `UPDATE`, scoped through `Requirement` since `TestPlan` has no direct `sprint_id`). Ineligible plans (`pending`, `generating`, already `approved`, `failed`) are left untouched. **Response** (200): `list[TestPlanResponse]` — the sprint's full plan list. **Errors:** 404 (sprint), 422 (finished sprint).

#### `POST /api/test-plans/{plan_id}/restart`

Restart a `failed` plan (clears the error and retry counter; keeps pending feedback so an interrupted revision resumes; uncapped).

**Errors:** 404, 422 (not `failed`, finished sprint).

### Test Execution

The fourth and final sprint stage, available once every requirement's plan is `approved` (`test_plans_complete`). Each run covers one or more requirements: one `TestExecution` row (and RQ job) per selected requirement, each walking that requirement's approved test cases in order — reusing a cached script per case or generating one, executing it in a subprocess with the confirmed environment variables injected, and self-healing script bugs via an LLM diagnosis loop (capped). Lifecycle per execution: `pending → running → completed` (terminal), plus `failed` (restartable). Per-case outcomes: `passed`, `failed` (a genuine application bug), or `error` (self-heal exhausted, still looks like a script bug). Generated scripts may use Playwright, `requests`, `Faker`, `psycopg2` (Postgres), `sqlite3`, or the standard library — nothing else, since only those are installed in the worker's own venv that scripts execute under.

> **Unsandboxed execution:** generated test scripts run as a plain subprocess with no sandboxing beyond a wall-clock timeout (`SCRIPT_EXECUTION_TIMEOUT`) — an accepted risk, not an oversight.

#### `POST /api/sprints/{sprint_id}/test-runs`

Create a run covering the selected requirements. Body: `{ "requirement_ids": [1, 2, …] }`. First best-effort refreshes the sprint's README/file-tree context from GitHub exactly once for the whole run — so scripts are generated/self-healed against current repo state rather than the (possibly stale) sprint-creation-time snapshot — skipping the README refresh when the sprint's README was user-uploaded (`Sprint.readme_user_provided`; a user-supplied README is authoritative) and never blocking run creation on a GitHub failure. Then creates one `TestRun` plus one `TestExecution` (and one `TestCaseExecution` per plan case, in position order) per requirement, enqueued best-effort.

**Response** (201): `TestRunDetailResponse` — `{ "id", "sprint_id", "created_at", "status", "executions": [...] }`, where each execution is `{ "id", "requirement_id", "requirement_name", "status", "error", "cases": [...], "created_at", "updated_at" }` and each case is `{ "id", "test_case", "status", "attempts", "output", "error", "updated_at" }`.

**Errors:** 404 (sprint), 422 (finished sprint, empty selection, a selected id isn't a confirmed requirement of this sprint, a selected requirement's plan isn't `approved`, or a selected requirement already has a run in progress — offending requirements are named in `detail`).

#### `GET /api/sprints/{sprint_id}/test-runs`

List a sprint's runs, newest first. Each row includes rolled-up `status`, `requirement_names`, and case counts (`total_cases`, `passed_cases`, `failed_cases`, `error_cases`). 404 on unknown sprint.

#### `GET /api/test-runs/{run_id}`

Fetch one run's full detail (same shape as the create response). 404 if not found.

#### `GET /api/test-case-executions/{id}/script`

Download the exact script that produced this case's result as a `.py` file attachment — credential-free by construction (scripts only ever read `os.environ["NAME"]`). 404 if the row or its script doesn't exist yet.

#### `POST /api/test-executions/{execution_id}/restart`

Restart a `failed` execution (uncapped). Case-level resumability is automatic — the task skips already-finalized cases and resumes from the first non-finalized one.

**Errors:** 404, 422 (not `failed`, finished sprint).

### Exploratory Testing

The fifth sprint stage, sharing the test-runs page with scripted runs and the same gate (`test_plans_complete`). Instead of executing predefined cases, an LLM drives a real Playwright browser against the running application to look for what the test plan didn't anticipate.

Two frameworks shape it. **SBTM** (Session-Based Test Management) organizes exploration into time-boxed _sessions_, each governed by a single _charter_ and producing a reviewable _session sheet_. **SFDIPOT** (Structure, Function, Data, Interfaces, Platform, Operations, Time) is the heuristic used to generate charters that attack a requirement from genuinely different angles.

A run covers **exactly one requirement** — unlike a scripted run, which can batch several. Exploration is expensive and meant to be read, not glanced at. Within a run, charters execute **sequentially** in a single RQ job: charters for one requirement touch the same feature by construction, so running them concurrently would collide on the same records and manufacture false findings.

Lifecycle per run: `pending → running → completed` (terminal), plus `failed` (restartable). Per session: `pending → running → completed`, plus `error` when the session machinery itself broke. A session that finds twenty bugs is still `completed` — findings drive their own counts, not the status.

Findings are typed, following SBTM's distinction:

- **bug** — the product behaves differently from what the requirement says
- **issue** — something obstructed the testing itself (missing credentials, an unreachable page)

Each carries reproduction steps, expected vs actual, and a screenshot captured at the moment it was recorded.

> **Credentials never reach the transcript.** The agent types secrets via a `fill_secret(ref, env_var_name)` tool whose value is resolved inside the executor, so no literal password or token enters the LLM conversation or the stored action log.

> **Navigation is origin-locked** to the application URLs nominated during charter generation, and re-checked after each navigation settles so a redirect chain can't escape. Everything else — restraint around destructive actions — is prompt-level guidance only, and exploration runs unsandboxed against your test environment.

> **Screenshots follow `STORE_OFFLINE`.** With it disabled, findings simply carry no screenshot; that's the documented behaviour of the setting, not a failure.

#### `POST /api/sprints/{sprint_id}/exploratory-charters/generate`

Draft charters for one requirement. Body: `{ "requirement_id": 1 }`. Runs a single **synchronous** LLM call — the model is shown the requirement, its approved test cases (as "already covered"), the README, the file tree, and the _names_ of the environment variables. It returns charters tagged with the SFDIPOT dimensions that apply and nominates which variables hold application URLs the browser may reach. **Nothing is persisted.**

**Response** (200): `{ "requirement_id", "requirement_name", "charters": [{ "charter", "sfdipot_areas" }], "base_url_env_vars", "charter_count", "projected_minutes" }`. `projected_minutes` is a heuristic derived from the charter count and `EXPLORATORY_MAX_ACTIONS` — the model is never asked to estimate duration.

**Errors:** 404 (sprint), 422 (finished sprint, requirement not confirmed, plan not `approved`, test environment not `confirmed`), 502 (LLM failure, or a nominated variable that doesn't exist or doesn't hold an `http(s)` URL).

#### `POST /api/sprints/{sprint_id}/exploratory-runs`

Start a run over the reviewed charters. Body: `{ "requirement_id", "charters": [{ "charter", "sfdipot_areas" }], "base_url_env_vars" }`. The charters and URL variables come back possibly edited, so **everything is re-validated from scratch** — nothing the generate call returned is trusted. Best-effort refreshes README/file tree once, then creates one `ExploratoryRun` plus one `ExploratorySession` per charter and enqueues a single job.

**Response** (201): `ExploratoryRunDetailResponse` — `{ "id", "sprint_id", "requirement_id", "requirement_name", "status", "summary", "error", "base_url_env_vars", "sessions": [...], "bug_count", "issue_count", "high_severity_count", "created_at", "updated_at" }`.

**Errors:** 404 (sprint), 422 (finished sprint, requirement not confirmed, plan not `approved`, environment not `confirmed`, no charters, a blank charter, more than `EXPLORATORY_MAX_CHARTERS`, an unknown SFDIPOT area, a URL variable that doesn't exist or isn't an `http(s)` URL, or the requirement already has a run in progress).

#### `GET /api/sprints/{sprint_id}/exploratory-runs`

List a sprint's exploratory runs, newest first, with `requirement_name`, `status`, `summary`, and finding counts. 404 on unknown sprint.

#### `GET /api/exploratory-runs/{run_id}`

Fetch one run's detail, including its session list. 404 if not found.

#### `GET /api/exploratory-sessions/{session_id}`

Fetch one charter's full SBTM session sheet: charter, SFDIPOT areas, actions used, stop reason, test notes, findings, and the complete action log. 404 if not found.

#### `GET /api/exploratory-findings/{finding_id}/screenshot`

Serve the PNG captured when the finding was recorded. 404 when the finding has none (the normal case with `STORE_OFFLINE` disabled) or the file is gone.

#### `POST /api/exploratory-runs/{run_id}/restart`

Restart a `failed` run (uncapped). Charter-level resumability is automatic: already-`completed` sessions are skipped and the in-flight charter restarts from scratch, since a half-explored browser died with the worker. Findings already recorded are kept — they were real observations regardless.

**Errors:** 404, 422 (not `failed`, finished sprint).

#### `POST /api/exploratory-runs/{run_id}/summarize`

Regenerate the per-requirement summary. The summary is written best-effort at the end of a run, so one transient provider failure can leave it null; this retries it synchronously. Works whether or not a summary already exists.

**Errors:** 404, 422 (run isn't `completed`), 502 (LLM failure — any existing summary is left untouched).

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
  migrations.py        # Idempotent startup migrations (run after init_db)
  worker.py            # RQ worker CLI (python -m backend.worker)
  models/
    database.py        # Table models: Repo, Sprint, Requirement, TestEnvironmentAccess, TestPlan, TestCase, TestRun, TestExecution, TestCaseExecution
    types.py           # Request/response types
  routes/
    auth.py            # POST /api/auth/verify, GET /api/auth/check
    repos.py           # Repo registration, listing, deactivation, README status
    sprints.py         # Sprint create/list/get/finish
    requirements.py    # Requirement CRUD, PRD upload/split + clarification/confirm/restart
    test_environment.py # Test environment get/submit/answer/confirm (synchronous LLM check) + env-var extraction/edit
    test_plans.py      # Test plan generate/list/feedback/edit/approve/restart
    test_execution.py  # Test run create/list/detail, script download, restart
  services/
    storage.py         # Conditional README/PRD persistence (STORE_OFFLINE)
    queue.py           # RQ queue service (graceful degradation when Redis is down)
    llm.py             # OpenAI-SDK client: clarity/test-env checks, env-var extraction, PRD split, test-plan + test-script tool loops
    llm_prompts.py      # System prompts, prompt-assembly helpers, TestCaseLike, read_file tool schema
    script_runner.py    # Subprocess execution of generated test scripts (no sandboxing beyond a timeout)
    reconciler.py      # Re-enqueues lost jobs, sweeps crashed-worker heartbeats (requirements + plans + executions)
  tasks/
    analyze_requirement.py  # The analysis task executed by the worker
    generate_test_plan.py   # The plan-generation task (bounded read_file tool loop)
    execute_test.py         # The test-execution task (per-case self-heal loop)
  scripts/
    clear_queue.py     # Queue maintenance CLI (python -m backend.scripts.clear_queue)
    reset_db.py        # Drop + recreate all tables (python -m backend.scripts.reset_db)
  utils/
    auth.py            # verify_auth cookie dependency
    crypto.py          # Fernet encryption for GitHub tokens
    github_utils.py    # GitHub API client and error hierarchy
    prd_utils.py       # PRD text extraction (.md/.txt via UTF-8, .pdf via pypdf, .docx via python-docx)
    readme_utils.py    # Best-effort README resolution (stored copy → re-download → none) + forced README/file-tree refresh
    sprint_utils.py    # Unique sprint directory generation
  tests/               # pytest suite (in-memory SQLite, mocked GitHub API, Redis + LLM stubbed)
```

## Requirements

- Python 3.10+
- PostgreSQL
- Redis (for requirement analysis, test-plan generation, test execution, and exploratory testing; optional otherwise)
- An LLM API key (`OPENAI_API_KEY` — for requirement analysis, the test-environment check, test-plan generation, test execution, and exploratory testing)
- For test execution and exploratory testing: `playwright install chromium` on the worker host (one-time)
- Dependencies declared in `pyproject.toml` (install with `pip install -e ".[dev]"` from the repo root)
- If using conda, start `python -m backend.worker` from an **activated** environment — generated test scripts inherit the worker process's own environment as-is, so an unactivated worker means an unactivated (and potentially broken) script environment too
