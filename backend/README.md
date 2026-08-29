# QA Agent Backend

FastAPI + PostgreSQL backend for the QA Agent — manages GitHub repositories and QA sprints. Registering a repo validates it against the GitHub API; creating a sprint downloads the repo's README (or accepts an uploaded one) and captures a filtered file-tree listing, both stored as LLM context. Sprint requirements are entered manually or extracted from an uploaded PRD document (split into requirements by a synchronous LLM call), then analyzed for QA-clarity by an LLM via Redis/RQ background workers, with a clarification question/answer loop per requirement. Once every requirement is confirmed, the user describes test environment access in free text — judged synchronously by the LLM, which also extracts the access details into structured, editable environment variables — and confirming it opens the test-planning stage. Next, an LLM generates a test plan per requirement on the same worker infrastructure — grounded in the requirement rather than the implementation, and written for automated execution; each draft plan goes through a capped feedback loop or uncapped direct edit until approved. Finally, running the approved plans generates (or reuses) a Playwright script per test case, reading repository files through a bounded tool loop to get real endpoints and response shapes right, executes it in a subprocess against the confirmed environment, and self-heals script bugs via an LLM diagnosis loop — stopping as soon as a failure looks like a genuine application bug and reporting it as a structured finding, in the same shape exploratory testing produces. Every confirmed artifact stays editable: correcting a requirement removes its test plan and sends the environment back for re-checking, changing the environment removes the sprint's plans, and editing an approved plan returns it to draft. Test runs that already executed are never deleted — they are kept and marked as out of date, naming which artifact moved.

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

### Background worker (requirement analysis + test-plan generation + test execution + exploratory testing + nonfunctional testing)

Requirement clarity analysis, test-plan generation, test execution, exploratory testing, and nonfunctional testing run on RQ workers backed by Redis (every task type shares the same queue and workers). The API works without them — rows just stay `pending` until a worker picks them up (a reconciler in the API process re-enqueues the backlog automatically when Redis recovers).

```bash
# Terminal 2 — start a worker (repo root; reads the same backend/.env)
python -m backend.worker

# Start more workers in additional terminals for concurrency.

# Maintenance: empty the queue and job registries
python -m backend.scripts.clear_queue
```

On Windows the worker automatically uses RQ's `SimpleWorker` (no `os.fork()`). The worker also needs an LLM key: set `OPENAI_API_KEY` (and optionally `OPENAI_BASE_URL` / `OPENAI_MODEL` for any OpenAI-compatible provider; the defaults target DeepSeek). The same key powers the test-environment sufficiency check, which runs synchronously inside the API request — no worker involved. Test execution, exploratory testing and nonfunctional testing additionally need the Playwright browser binary on the worker host — run `playwright install chromium` once (the `playwright` pip package alone doesn't include it). Both browser-driven modes drive that browser directly from the worker process (not a subprocess), so they must run headless unless you set `EXPLORATORY_HEADLESS=false` to watch them. If you use conda, always start the worker (`python -m backend.worker`) from an **activated** environment — generated test scripts run via `subprocess.run([sys.executable, ...])`, which guarantees the same interpreter/site-packages regardless of activation state, but inherits the worker's own `os.environ` unchanged, so PATH-dependent behavior is only as correct as however the worker process itself was started.

Generated test scripts may import Playwright, `requests`, `Faker`, `psycopg2` (PostgreSQL), `sqlite3`, and the Python standard library — this set is advertised to the LLM in the script-generation and diagnosis prompts (`llm_prompts.AVAILABLE_TEST_LIBRARIES`) so it doesn't guess at unavailable packages.

## Environment Variables

`.env.example` documents every variable. The important ones:

| Variable                                                                               | Default                                                  | Description                                                                                                                                                                                                                                                                                           |
| -------------------------------------------------------------------------------------- | -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DATABASE_URL`                                                                         | `postgresql://postgres:postgres@localhost:5432/qa_agent` | PostgreSQL connection string.                                                                                                                                                                                                                                                                         |
| `ENCRYPTION_KEY`                                                                       | _(unset)_                                                | Fernet key for encrypting GitHub tokens, issue-tracker API tokens **and CI/CD write tokens** at rest. Required to register repos with a token, to connect an issue tracker, and to connect CI/CD export. The CI/CD token authorizes commits, so it is the highest-privilege credential the app holds. |
| `APP_PASSWORD`                                                                         | _(unset)_                                                | Shared password for accessing the QA Agent UI. When unset, authentication is disabled.                                                                                                                                                                                                                |
| `STORE_OFFLINE`                                                                        | `false`                                                  | Set to `"true"` to persist sprint READMEs, uploaded PRDs and run screenshots to disk. With it off, findings simply carry no screenshot.                                                                                                                                                               |
| `STORAGE_LOCATION`                                                                     | `./uploads`                                              | Directory for sprint files when `STORE_OFFLINE=true`.                                                                                                                                                                                                                                                 |
| `CORS_ORIGINS`                                                                         | `http://localhost:5173`                                  | Comma-separated list of allowed origins.                                                                                                                                                                                                                                                              |
| `GITHUB_API_TIMEOUT`                                                                   | `15`                                                     | Timeout in seconds for GitHub API requests.                                                                                                                                                                                                                                                           |
| `FILE_TREE_MAX_CHARS`                                                                  | `20000`                                                  | Character cap for the repo file-tree listing captured at sprint creation.                                                                                                                                                                                                                             |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` / `REDIS_DB`                            | `localhost` / `6379` / _(unset)_ / `0`                   | Redis connection for the analysis queue.                                                                                                                                                                                                                                                              |
| `JOB_TIMEOUT` / `JOB_RESULT_TTL` / `WORKER_TTL`                                        | `300` / `3600` / `30`                                    | RQ job timeout, result retention, and worker heartbeat TTL (bounds Windows shutdown lag).                                                                                                                                                                                                             |
| `OPENAI_API_KEY`                                                                       | _(unset)_                                                | LLM API key. Required for every LLM stage — requirement analysis, the test-environment check, test-plan generation, test execution, exploratory testing and nonfunctional testing; never logged or returned.                                                                                          |
| `OPENAI_BASE_URL`                                                                      | `https://api.deepseek.com`                               | Any OpenAI-compatible provider.                                                                                                                                                                                                                                                                       |
| `OPENAI_MODEL`                                                                         | `deepseek-v4-flash`                                      | Model used for the LLM checks.                                                                                                                                                                                                                                                                        |
| `OPENAI_TIMEOUT`                                                                       | `60`                                                     | Timeout in seconds for LLM requests.                                                                                                                                                                                                                                                                  |
| `MAX_CLARIFICATION_ROUNDS`                                                             | `3`                                                      | Clarification Q&A rounds per requirement; past the cap only confirm-as-is or manual edit.                                                                                                                                                                                                             |
| `MAX_AUTO_RETRIES`                                                                     | `3`                                                      | Automatic retries before a requirement is marked `failed`; manual Restart stays uncapped.                                                                                                                                                                                                             |
| `PRD_MAX_CHARS`                                                                        | `50000`                                                  | Character cap on text extracted from an uploaded PRD; larger uploads are rejected (422), never truncated.                                                                                                                                                                                             |
| `MAX_PRD_REQUIREMENTS`                                                                 | `50`                                                     | Max requirements a single PRD split may produce; larger splits are rejected (422).                                                                                                                                                                                                                    |
| `MAX_TEST_ENV_REVISION_ROUNDS`                                                         | `3`                                                      | Answer/revise rounds for the test-environment text; direct edit stays uncapped.                                                                                                                                                                                                                       |
| `MAX_TEST_PLAN_FEEDBACK_ROUNDS`                                                        | `3`                                                      | Feedback/revise rounds per test plan; direct edit stays uncapped.                                                                                                                                                                                                                                     |
| `TEST_PLAN_JOB_TIMEOUT`                                                                | `900`                                                    | RQ job timeout for plan jobs. Generation is a single LLM call, so this is vestigial headroom rather than a sized budget.                                                                                                                                                                              |
| `MAX_SCRIPT_FIX_ROUNDS`                                                                | `3`                                                      | Additional self-heal attempts per test case before a stubborn `script_bug` verdict gives up (case ends `error`, not `failed`).                                                                                                                                                                        |
| `TEST_EXECUTION_TOOL_ROUNDS`                                                           | `5`                                                      | Max `read_file` LLM rounds per test-script generation/diagnosis call — the only stage with repository access.                                                                                                                                                                                         |
| `TEST_EXECUTION_FILE_MAX_CHARS`                                                        | `20000`                                                  | Per-file character cap for repo files fetched by that tool loop.                                                                                                                                                                                                                                      |
| `SCRIPT_EXECUTION_TIMEOUT`                                                             | `60`                                                     | Wall-clock timeout in seconds for one test-script subprocess run.                                                                                                                                                                                                                                     |
| `TEST_EXECUTION_JOB_TIMEOUT`                                                           | `3600`                                                   | RQ job timeout for test-execution jobs — sized for every case in a plan, each with multiple generate/execute/diagnose cycles.                                                                                                                                                                         |
| `EXPLORATORY_MAX_ACTIONS` / `EXPLORATORY_MAX_CHARTERS`                                 | `25` / `6`                                               | SBTM time box in LLM tool rounds, and charters per run. `MAX_ACTIONS` is the main wall-clock lever — charters run serially, so a run costs the sum of its sessions.                                                                                                                                   |
| `EXPLORATORY_MAX_FINDINGS`                                                             | `20`                                                     | Findings per session. Also the free-recording budget: `record_finding` does not consume an action until this cap.                                                                                                                                                                                     |
| `EXPLORATORY_SNAPSHOT_WINDOW` / `_SNAPSHOT_MAX_CHARS`                                  | `3` / `20000`                                            | Verbatim page snapshots kept in the conversation, and the per-snapshot character cap. Together they set the floor context compaction cannot go below.                                                                                                                                                 |
| `EXPLORATORY_CONTEXT_TOKEN_LIMIT`                                                      | `40000`                                                  | Prompt-token size at which a session compacts its own history.                                                                                                                                                                                                                                        |
| `EXPLORATORY_ACTION_TIMEOUT` / `_SECONDS_PER_ACTION`                                   | `10` / `8`                                               | Per-Playwright-action timeout, and the **display-only** figure behind the pre-run duration estimate.                                                                                                                                                                                                  |
| `EXPLORATORY_JOB_TIMEOUT` / `EXPLORATORY_HEADLESS`                                     | `7200` / `true`                                          | RQ job timeout covering every charter serially, and headed mode for local debugging.                                                                                                                                                                                                                  |
| `NONFUNCTIONAL_MAX_TARGETS` / `_MAX_ACTIONS`                                           | `10` / `30`                                              | URLs examined per run, and navigation tool rounds per run. Reaching the target cap stops new URLs being examined but does not end the walk — the model may still need to pass through pages to reach something.                                                                                       |
| `NONFUNCTIONAL_MAX_FINDINGS`                                                           | `50`                                                     | Findings persisted per run, across every URL and domain.                                                                                                                                                                                                                                              |
| `NONFUNCTIONAL_AXE_MAX_CHARS` / `_TRIAGE_MAX_CHARS`                                    | `20000` / `60000`                                        | Per-URL axe payload cap, and the cap on one batched triage call above which the batch is chunked. Without the second, ten URLs of axe output would go out as one request and time out — silently, because every violation already carries fallback text.                                              |
| `NONFUNCTIONAL_CATALOGUE_TIMEOUT`                                                      | `30`                                                     | Wall clock for one URL's whole catalogue. A domain the budget cuts off records `failed_to_run`, never silence.                                                                                                                                                                                        |
| `NONFUNCTIONAL_JOB_TIMEOUT`                                                            | `5400`                                                   | RQ job timeout covering the walk, every URL's catalogue, every load profile serially, and triage.                                                                                                                                                                                                     |
| `NONFUNCTIONAL_MAX_LOAD_PROFILES`                                                      | `3`                                                      | Load profiles per run.                                                                                                                                                                                                                                                                                |
| `NONFUNCTIONAL_LOAD_MAX_CONCURRENCY` / `_MAX_DURATION_SECONDS` / `_MAX_TOTAL_REQUESTS` | `10` / `60` / `2000`                                     | The **safe** tier: GET/HEAD/OPTIONS only read, so they run against any confirmed origin under these ceilings.                                                                                                                                                                                         |
| `NONFUNCTIONAL_LOAD_UNSAFE_MAX_CONCURRENCY` / `_UNSAFE_MAX_TOTAL_REQUESTS`             | `2` / `20`                                               | The **non-safe** tier: POST/PUT/PATCH/DELETE change data, so they run only on a run carrying the disposable-environment declaration. The total is the binding cap and is deliberately in the tens.                                                                                                    |
| `NONFUNCTIONAL_LOAD_REQUEST_TIMEOUT` / `_ERROR_RATE_STOP`                              | `15` / `0.5`                                             | One outbound load request, and the error rate above which a profile stops early rather than keeping traffic on a host that is already failing.                                                                                                                                                        |
| `ISSUE_TRACKER_TIMEOUT`                                                                | `15`                                                     | Timeout in seconds for one outbound issue-tracker request (verify, create issue, state check, attachment). There is deliberately no cap on how many issues a run may file — grouping is what bounds that.                                                                                             |
| `CICD_TOOL_ROUNDS`                                                                     | `5`                                                      | Max `read_file` LLM rounds per CI/CD generation call. Reading the repository _is_ the task here, so it gets real room to look around.                                                                                                                                                                 |
| `CICD_EXPORT_JOB_TIMEOUT` / `CICD_MAX_WORKFLOWS`                                       | `1800` / `20`                                            | RQ job timeout for one export (one LLM call plus a handful of GitHub requests), and how many existing workflow files an export fetches and parses.                                                                                                                                                    |
| `RECONCILER_INTERVAL`                                                                  | `30`                                                     | Seconds between reconciler ticks (re-enqueues lost/backlogged jobs).                                                                                                                                                                                                                                  |
| `HEARTBEAT_STALE_SECONDS`                                                              | `180`                                                    | Age after which an `analyzing` heartbeat counts as a crashed worker; keep above `OPENAI_TIMEOUT`.                                                                                                                                                                                                     |
| `PENDING_JOB_STALE_SECONDS`                                                            | `30`                                                     | Age after which a `pending` row's started RQ job counts as a crashed worker.                                                                                                                                                                                                                          |

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
  "created_at": "2026-07-13T12:00:00Z",
  "has_access_token": false
}
```

The access token is never returned in responses — only `has_access_token`, so the issue-tracker form can say whether the repo can supply a credential instead of the user learning it from a save that fails.

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
  "environment_confirmed": false,
  "has_test_plans": false,
  "test_plans_missing": false,
  "test_plans_complete": false,
  "has_test_runs": false
}
```

The boolean flags are computed by the backend: `requirements_complete` (≥1 requirement and all `confirmed`), `has_test_environment_submission` (a test-environment row exists), `environment_confirmed` (the test environment is confirmed — the precondition for generating test plans), `has_test_plans` (≥1 requirement has a test-plan row), `test_plans_missing` (a `confirmed` requirement has _no_ plan — what a requirement edit leaves behind, and invisible in the plan list itself), `test_plans_complete` (every requirement has an `approved` plan), and `has_test_runs` (≥1 test run has been submitted).

**Errors:** 404 (repo not found), 422 (empty name, deactivated repo, invalid README, or no README available), 502 (GitHub API failure).

#### `GET /api/sprints`

List sprints — active first, newest first within each group. Supports `offset` / `limit`.

#### `GET /api/sprints/{sprint_id}`

Get a single sprint with its repo info. 404 if not found.

#### `PATCH /api/sprints/{sprint_id}`

Finish a sprint. Body: `{ "active": false }` (the only supported transition). Any `pending`/`analyzing` requirements, `pending`/`generating` test plans, and `pending`/`running` test executions, exploratory runs and nonfunctional runs are marked `failed` — nothing runs on a finished sprint. The test cases, charter sessions, examined URLs and load profiles those runs never reached are marked `skipped` in the same commit, so nothing is left reading as queued under a run that can no longer proceed.

**Errors:** 404 (not found), 422 (already finished or `active` not `false`).

### Requirements

Requirements belong to a sprint and carry a lifecycle status: `pending → analyzing → needs_clarification ⇄ analyzing → ready → confirmed`, plus `failed` (restartable). Analysis happens on the background worker; poll the list endpoint to observe progress.

#### `POST /api/sprints/{sprint_id}/requirements`

Create a batch of requirements (JSON body: `[{ "name": "...", "description": "..." }, …]`). Rows start `pending` and are enqueued for analysis. Adding a requirement to a sprint whose test environment is already `confirmed` sends that environment back to `ready` for re-checking — a new requirement may need access the confirmed description never covered. **Errors:** 404 (sprint), 422 (empty list, blank fields, finished sprint).

#### `POST /api/sprints/{sprint_id}/requirements/from-prd`

Upload a PRD document and have an LLM split it into requirements — the alternative to entering them manually. The split runs **synchronously inside the request** (offloaded to a thread, bounded by `OPENAI_TIMEOUT`); the resulting rows start `pending` with `from_prd: true` and enter the normal analysis pipeline.

**Request:** `multipart/form-data`

| Field      | Required | Description                                                           |
| ---------- | -------- | --------------------------------------------------------------------- |
| `prd_file` | Yes      | `.md` / `.markdown` / `.txt` (UTF-8), `.pdf`, or `.docx` PRD document |

**Response** (201): `list[RequirementResponse]` — the newly created rows.

Re-uploading a PRD **replaces** the previous upload's `from_prd` rows (in the same transaction as the new inserts); manually entered requirements are never touched. Every failure — invalid file, unreadable/empty document, text over `PRD_MAX_CHARS`, zero or more than `MAX_PRD_REQUIREMENTS` extracted requirements, LLM failure — happens before that transaction, so a failed upload never destroys existing requirements. When `STORE_OFFLINE=true` the original file is saved to the sprint directory as `PRD<ext>` (best-effort).

A re-upload is simultaneously a bulk delete and a bulk add, so both cascades apply: the superseded rows lose their test plans, and the test environment goes back for re-checking. **Errors:** 404 (sprint), 422 (finished sprint, unsupported/corrupt/empty/oversized file, no requirements found, too many requirements), 502 (LLM failure — nothing persisted).

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

Remove a requirement (204). Allowed in **every** status, including `confirmed`, and after the test environment is confirmed. Removes that requirement's test plan but leaves the environment `confirmed` — removal can only shrink what needs access. A requirement referenced by a test run or exploratory run is _archived_ rather than deleted so those runs stay readable; one with no runs behind it is deleted outright. 422 on finished sprints. Work already in flight does not block the removal — the worker stops itself instead (see the run-staleness note under test runs).

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

Resubmitting a **confirmed** description is allowed and re-runs the sufficiency check. If the description changed, or the check now comes back insufficient (which clears the variables), every test plan in the sprint is removed and existing runs are marked out of date. Resubmitting **identical** text changes nothing and costs only the one check call: the variable extraction is skipped entirely, since the variables are derived from a description that did not move. That is what makes the UI's Re-check button (offered when a requirement changed since the last check) safe to press — a fresh extraction is non-deterministic, and re-running it would let harmless rewording delete the sprint's plans and silently overwrite any variables you corrected by hand. **Errors:** 404 (sprint), 422 (finished sprint, requirements not all confirmed, or empty content), 502 (LLM failure — nothing is persisted).

#### `POST /api/test-environment/{te_id}/answer`

Answer the clarifying question (`{ "answer": "..." }`); the LLM rewrites the description and re-judges it. Capped at `MAX_TEST_ENV_REVISION_ROUNDS` (default 3) — past the cap this returns 422 and the text must be edited directly (re-POST, uncapped).

**Errors:** 404, 422 (not `needs_info`, cap reached, empty answer, finished sprint, requirements incomplete), 502 (LLM failure).

#### `PATCH /api/test-environment/{te_id}/env-vars`

Directly correct the LLM-extracted variables — no LLM call, uncapped, doesn't touch `content`/`status`/`revision_count`. Body: `{ "variables": { "NAME": "value", … } }`.

Editable after confirmation: changing the variables removes every test plan in the sprint, returns the row to `ready` for re-confirmation, and marks existing runs out of date. `updated_at` is deliberately _not_ stamped — on this row it means "last LLM check" and drives `requirements_stale`. **Errors:** 404, 422 (finished sprint, empty `variables`, or a blank name/value).

#### `POST /api/test-environment/{te_id}/confirm`

Finalize the access description. This is the precondition for generating test plans (`SprintResponse.environment_confirmed`). Not terminal: the description and its variables stay editable, and adding a requirement returns the row to `ready` for re-checking.

**Errors:** 404, 422 (not `ready`, finished sprint, requirements incomplete, `requirements_stale` — a confirmed requirement changed since the last check; re-POST the current content to re-check first — or environment variables not yet extracted, which should be unreachable).

### Test Plans

The third sprint stage, available once the test environment is confirmed (`environment_confirmed`). One plan per confirmed requirement, generated asynchronously on the RQ worker by a single LLM call returning a structured plan — complexity (`low`/`medium`/`high`), summary, and ≥1 test case (title, optional preconditions, newline-joined steps, expected result, type, priority).

Planning is deliberately **code-blind**: it is grounded in the requirement, README, captured file tree, and confirmed test-environment description, but never reads repository files. A plan defines what "correct" means, so reading the implementation is where that judgment drifts into restating what the code already does; the endpoint paths and response shapes a script needs are resolved later by test-script generation, which does have repo access. The prompt instead states what it is planning for — each case becomes one Playwright script, so expected results must be script-checkable, preconditions must be seedable from the confirmed environment, cases must be repeatable, and steps describe _what_ to verify rather than naming endpoints or selectors. Checks no script could make are left to exploratory testing.

Lifecycle: `pending → generating → draft ⇄ generating (feedback revision) → approved`, plus `failed` (restartable). Approval gates _running_, not editing — an approved plan can still be edited or given feedback, which returns it to `draft`. Poll the list endpoint to observe progress.

#### `POST /api/sprints/{sprint_id}/test-plans/generate`

Create a `pending` plan for every confirmed requirement and enqueue one generation job each. Idempotent — requirements that already have a plan are skipped, `failed` plans are reset like Restart (keeping any interrupted feedback), and the sprint's full plan list is returned either way.

This is also how a plan removed by a requirement edit comes back: editing a confirmed requirement deletes the plan written against its old text and nothing regenerates it automatically. Once that requirement is confirmed again, `SprintResponse.test_plans_missing` flips to `true` and this endpoint rebuilds only the missing plan, leaving existing ones untouched.

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

**Errors:** 404, 422 (not `draft` or `approved`, cap reached, empty feedback, finished sprint).

#### `PATCH /api/test-plans/{plan_id}`

Directly edit a `draft` plan — no LLM involved, uncapped, never increments `revision_count`, stays `draft`. Body: `{ "complexity": "low|medium|high", "summary": "...", "cases": [{ "title": "...", "preconditions": null, "steps": "one step per line", "expected_result": "...", "case_type": "...", "priority": "high|medium|low" }, …] }`. Cases are replaced wholesale.

**Errors:** 404, 422 (not `draft` or `approved`, finished sprint, or field validation: no cases, blank title/steps/expected result/type, invalid priority/complexity).

#### `POST /api/test-plans/{plan_id}/approve`

Approve a `draft` plan. There is no unapprove, but editing an approved plan returns it to `draft` and requires approving again. When every requirement's plan is approved, `SprintResponse.test_plans_complete` flips to `true`.

**Errors:** 404, 422 (already approved, not `draft`, finished sprint).

#### `POST /api/sprints/{sprint_id}/test-plans/approve-all`

Approve every currently `draft` plan in the sprint in one request (a single set-based `UPDATE`, scoped through `Requirement` since `TestPlan` has no direct `sprint_id`). Ineligible plans (`pending`, `generating`, already `approved`, `failed`) are left untouched. **Response** (200): `list[TestPlanResponse]` — the sprint's full plan list. **Errors:** 404 (sprint), 422 (finished sprint).

#### `POST /api/test-plans/{plan_id}/restart`

Restart a `failed` plan (clears the error and retry counter; keeps pending feedback so an interrupted revision resumes; uncapped).

**Errors:** 404, 422 (not `failed`, finished sprint).

### Test Execution

The fourth and final sprint stage, available once every requirement's plan is `approved` (`test_plans_complete`). Each run covers one or more requirements: one `TestExecution` row (and RQ job) per selected requirement, each walking that requirement's approved test cases in order — reusing a cached script per case or generating one, executing it in a subprocess with the confirmed environment variables injected, and self-healing script bugs via an LLM diagnosis loop (capped). Lifecycle per execution: `pending → running → completed` (terminal), plus `failed` (restartable). Per-case outcomes: `passed`, `failed` (a genuine application bug), `error` (self-heal exhausted, still looks like a script bug), or `skipped` (the execution ended before reaching this case — see below). Generated scripts may use Playwright, `requests`, `Faker`, `psycopg2` (Postgres), `sqlite3`, or the standard library — nothing else, since only those are installed in the worker's own venv that scripts execute under.

Both terminal failures also carry a **structured finding** on `TestCaseExecutionResponse.finding`, in the same shape exploratory testing uses (severity, title, reproduction steps, expected vs actual, environment): a `failed` case reports a `bug`, an `error` case reports an `issue` — the product was wrong, versus the testing never got off the ground. The bug report costs no extra LLM call; the diagnosis that classified the failure returns it alongside the classification. A `passed` case reports no finding, and its finding fields are cleared, so a restarted run that now passes stops reporting a fixed bug. Raw `output`, `error`, and the downloadable script are unchanged — the finding is the report, the output is the debugging surface.

An execution can stop before it has walked every case — superseded by an upstream edit, a finished sprint, a plan no longer approved, or a worker that died with its retries exhausted. Whenever that happens the cases it never reached are marked `skipped` rather than left `pending`/`running`, and each carries a one-line `error` saying why: "Not run. …" for a case that never started, "Interrupted before it finished…" for the one that was in flight when a worker was killed (only that one may have partially touched the test environment). `skipped` is not a verdict about the product and is counted in none of `passed_cases`/`failed_cases`/`error_cases`; a restart re-runs those cases normally.

> **Unsandboxed execution:** generated test scripts run as a plain subprocess with no sandboxing beyond a wall-clock timeout (`SCRIPT_EXECUTION_TIMEOUT`) — an accepted risk, not an oversight.

#### `POST /api/sprints/{sprint_id}/test-runs`

Create a run covering the selected requirements. Body: `{ "requirement_ids": [1, 2, …], "export_findings": false }` — see [Issue Tracker](#issue-tracker) for the flag. First best-effort refreshes the sprint's README/file-tree context from GitHub exactly once for the whole run — so scripts are generated/self-healed against current repo state rather than the (possibly stale) sprint-creation-time snapshot — skipping the README refresh when the sprint's README was user-uploaded (`Sprint.readme_user_provided`; a user-supplied README is authoritative) and never blocking run creation on a GitHub failure. Then creates one `TestRun` plus one `TestExecution` (and one `TestCaseExecution` per plan case, in position order) per requirement, enqueued best-effort.

**Response** (201): `TestRunDetailResponse` — `{ "id", "sprint_id", "created_at", "status", "executions": [...] }`, where each execution is `{ "id", "requirement_id", "requirement_name", "status", "error", "cases": [...], "created_at", "updated_at" }` and each case is `{ "id", "test_case", "status", "attempts", "output", "error", "updated_at" }`.

**Errors:** 404 (sprint), 422 (finished sprint, empty selection, a selected id isn't a confirmed requirement of this sprint, a selected requirement's plan isn't `approved`, or a selected requirement already has a run in progress — offending requirements are named in `detail`).

#### `GET /api/sprints/{sprint_id}/test-runs`

List a sprint's runs, newest first. Each row includes rolled-up `status`, `requirement_names`, and case counts (`total_cases`, `passed_cases`, `failed_cases`, `error_cases`). 404 on unknown sprint.
Every run — scripted and exploratory — also carries `outdated_reasons` and `requirement_deleted`. A run records the content revisions of the requirement, test plan, and test environment it executed against; if any has since changed, the corresponding reason (`requirement`, `test_plan`, `test_environment`) appears. An empty list means the run still reflects the current sprint — there is no separate `outdated` boolean, since it would just be `outdated_reasons.length > 0`. `requirement_deleted` only selects the wording for the `requirement` reason (deletion is one of the ways a requirement can differ, not a separate state). **An outdated run cannot be restarted** — start a new one to test the current state. A run that goes outdated _while in progress_ stops itself at the next case (or charter) boundary and records `Superseded — …`, rather than spending LLM calls on a result already known to be stale. Editing is never blocked on a run being in flight.

#### `GET /api/test-runs/{run_id}`

Fetch one run's full detail (same shape as the create response). 404 if not found.

#### `GET /api/test-case-executions/{id}/script`

Download the exact script that produced this case's result as a `.py` file attachment — credential-free by construction (scripts only ever read `os.environ["NAME"]`). 404 if the row or its script doesn't exist yet.

Refused (422) when the execution is outdated — restarting would re-run against content it was never planned for.

#### `POST /api/test-executions/{execution_id}/restart`

Restart a `failed` execution (uncapped). Case-level resumability is automatic — the task skips already-finalized cases and resumes from the first non-finalized one.

**Errors:** 404, 422 (not `failed`, finished sprint).

### Exploratory Testing

The fifth sprint stage, sharing the test-runs page with scripted runs and the same gate (`test_plans_complete`). Instead of executing predefined cases, an LLM drives a real Playwright browser against the running application to look for what the test plan didn't anticipate.

Two frameworks shape it. **SBTM** (Session-Based Test Management) organizes exploration into time-boxed _sessions_, each governed by a single _charter_ and producing a reviewable _session sheet_. **SFDIPOT** (Structure, Function, Data, Interfaces, Platform, Operations, Time) is the heuristic used to generate charters that attack a requirement from genuinely different angles.

A run covers **exactly one requirement** — unlike a scripted run, which can batch several. Exploration is expensive and meant to be read, not glanced at. Within a run, charters execute **sequentially** in a single RQ job: charters for one requirement touch the same feature by construction, so running them concurrently would collide on the same records and manufacture false findings.

Lifecycle per run: `pending → running → completed` (terminal), plus `failed` (restartable). Per session: `pending → running → completed`, plus `error` when the session machinery itself broke and `skipped` when the run ended before that charter was ever explored (the mirror of a scripted `skipped` case — same reason text, same non-verdict meaning). A session that finds twenty bugs is still `completed` — findings drive their own counts, not the status.

Findings are typed, following SBTM's distinction:

- **bug** — the product behaves differently from what the requirement says
- **issue** — something obstructed the testing itself (missing credentials, an unreachable page)

Each carries a severity, reproduction steps, expected vs actual, the environment it was observed in, and a screenshot captured at the moment it was recorded. The environment names the browser and version, the viewport in effect at that instant, the host OS, and the page URL — a defect that reproduces at 375px wide and not at 1280px is a different defect. It is captured in code rather than asked of the model, which cannot see the browser build or host OS.

**Scripted test runs report findings in this same shape** — see [Test execution](#test-execution). Only the source differs: an exploratory session records one live through a tool, a scripted run derives one from a failed test case.

> **Credentials never reach the transcript.** The agent types secrets via a `fill_secret(ref, env_var_name)` tool whose value is resolved inside the executor, so no literal password or token enters the LLM conversation or the stored action log.

> **Navigation is origin-locked** to the application URLs nominated during charter generation, and re-checked after each navigation settles so a redirect chain can't escape. Everything else — restraint around destructive actions — is prompt-level guidance only, and exploration runs unsandboxed against your test environment.

> **Screenshots follow `STORE_OFFLINE`.** With it disabled, findings simply carry no screenshot; that's the documented behaviour of the setting, not a failure.

#### `POST /api/sprints/{sprint_id}/exploratory-charters/generate`

Draft charters for one requirement. Body: `{ "requirement_id": 1 }`. Runs a single **synchronous** LLM call — the model is shown the requirement, its approved test cases (as "already covered"), the README, the file tree, and the _names_ of the environment variables. It returns charters tagged with the SFDIPOT dimensions that apply and nominates which variables hold application URLs the browser may reach. **Nothing is persisted.**

**Response** (200): `{ "requirement_id", "requirement_name", "charters": [{ "charter", "sfdipot_areas" }], "base_url_env_vars", "charter_count", "projected_minutes" }`. `projected_minutes` is a heuristic derived from the charter count and `EXPLORATORY_MAX_ACTIONS` — the model is never asked to estimate duration.

**Errors:** 404 (sprint), 422 (finished sprint, requirement not confirmed, plan not `approved`, test environment not `confirmed`), 502 (LLM failure, or a nominated variable that doesn't exist or doesn't hold an `http(s)` URL).

#### `POST /api/sprints/{sprint_id}/exploratory-runs`

Start a run over the reviewed charters. Body: `{ "requirement_id", "charters": [{ "charter", "sfdipot_areas" }], "base_url_env_vars", "export_findings": false }` — see [Issue Tracker](#issue-tracker) for the flag. The charters and URL variables come back possibly edited, so **everything is re-validated from scratch** — nothing the generate call returned is trusted. Best-effort refreshes README/file tree once, then creates one `ExploratoryRun` plus one `ExploratorySession` per charter and enqueues a single job.

**Response** (201): `ExploratoryRunDetailResponse` — `{ "id", "sprint_id", "requirement_id", "requirement_name", "status", "summary", "error", "base_url_env_vars", "sessions": [...], "bug_count", "issue_count", "high_severity_count", "created_at", "updated_at" }`.

**Errors:** 404 (sprint), 422 (finished sprint, requirement not confirmed, plan not `approved`, environment not `confirmed`, no charters, a blank charter, more than `EXPLORATORY_MAX_CHARTERS`, an unknown SFDIPOT area, a URL variable that doesn't exist or isn't an `http(s)` URL, or the requirement already has a run in progress).

#### `GET /api/sprints/{sprint_id}/exploratory-runs`

List a sprint's exploratory runs, newest first, with `requirement_name`, `status`, `summary`, and finding counts. 404 on unknown sprint.

#### `GET /api/exploratory-runs/{run_id}`

Fetch one run's detail, including its session list. 404 if not found.

#### `GET /api/exploratory-sessions/{session_id}`

Fetch one charter's full SBTM session sheet: charter, SFDIPOT areas, actions used, stop reason, test notes, findings, and the complete action log. Also the polling endpoint for a live session (plain DB read) — `actions_used` is written every LLM round while the session runs, not once at the end. 404 if not found.

#### `GET /api/exploratory-findings/{finding_id}/screenshot`

Serve the PNG captured when the finding was recorded. 404 when the finding has none (the normal case with `STORE_OFFLINE` disabled) or the file is gone.

#### `POST /api/exploratory-runs/{run_id}/restart`

Restart a `failed` run (uncapped), provided it is not outdated. Charter-level resumability is automatic: already-`completed` sessions are skipped and the in-flight charter restarts from scratch, since a half-explored browser died with the worker. Findings already recorded are kept — they were real observations regardless.

**Errors:** 404, 422 (not `failed`, finished sprint).

#### `POST /api/exploratory-runs/{run_id}/summarize`

Regenerate the per-requirement summary. The summary is written best-effort at the end of a run and already retries itself once, so only a repeated provider failure leaves it null; this retries it synchronously (also twice). Works whether or not a summary already exists.

**Errors:** 404, 422 (run isn't `completed`), 502 (LLM failure — any existing summary is left untouched).

### Nonfunctional Testing

The third run mode, behind the same gate as the other two: every selected requirement needs an approved test plan and a confirmed test environment. Scope is **one requirement** per run.

What makes it different from the other two is where the judgment lives. An LLM walks the feature in a live browser, and at **every URL it lands on** a fixed catalogue runs automatically — accessibility (axe-core), passive security (a fixed rule table), single-request performance (read from the page that already loaded). The model is not asked whether something is wrong, is not told what the checks found, and **is not offered a tool to record a finding**: `NONFUNCTIONAL_TOOLS` carries navigation only, so an invented violation is not expressible rather than merely discouraged. Its second job, after the walk, is to write up in readable English what the tools already found and already graded.

The catalogue fires from an arrival hook rather than a tool call, deliberately: a tool the model can call is a tool it can decline to call, and "the full catalogue at every URL" would quietly become "wherever it remembered to look". The coverage floor is the confirmed environment's base URLs, examined before the walk begins; API endpoints are added from the browser's own XHR/fetch traffic and examined afterwards over plain HTTP.

Each URL records a **four-valued outcome per domain** — `clean`, `violations`, `not_applicable`, `failed_to_run` — because collapsing either of the last two into `clean` states something false. Accessibility does not apply to a JSON endpoint; an axe run that could not execute found nothing _because it never ran_. Performance is measured and stored and never judged: it produces no finding, no defect and no ticket, since any threshold would be a verdict on somebody else's capacity planning.

**Load profiles** run last, after the browser closes, safe methods first, carrying the browser's own cookies — so they exercise the application as a signed-in user rather than measuring the latency of a redirect to a login page. The consequence, which the UI states because the resulting data cannot: a non-safe profile performs up to `NONFUNCTIONAL_LOAD_UNSAFE_MAX_TOTAL_REQUESTS` authenticated writes as whoever the browser signed in as. Profiles are refused outright against loopback, link-local and private address space, redirects are not followed, and **a profile that already sent traffic is never re-sent** — restarting re-examines URLs freely, because re-reading a page costs a page load, but re-issuing requests costs real traffic on somebody's environment.

#### `POST /api/sprints/{sprint_id}/nonfunctional-plan/generate`

Propose which domains apply (with a one-line rationale each), which environment variables hold the application URLs, and which load profiles are worth running. Synchronous, one cheap LLM call, persists nothing. The response also carries **both ceiling tiers** and the list of safe methods, so the setup screen never restates a config value.

**Body:** `{ "requirement_id": int }`

**Errors:** 404, 422 (requirement not confirmed, no approved plan, environment not confirmed, finished sprint), 502 (LLM failure, or a nominated variable that does not hold an `http(s)` URL).

#### `POST /api/sprints/{sprint_id}/nonfunctional-runs`

Start a run. Everything the generate call proposed has been through a form by now, so **none of it is trusted**: domains are re-checked against the enum, base URLs must exist and hold `http(s)` URLs, and each load profile is re-checked for its origin, its method's tier, its `$NAME` placeholders and its ceilings. Concurrency, duration and total request count are **clamped** rather than refused — a run that quietly does less than asked is the safe direction — and the clamped values come back in the response.

**Body:** `{ "requirement_id": int, "domains": [str], "base_url_env_vars": [str], "load_profiles": [...], "environment_disposable": bool, "export_findings": bool }`

**Errors:** 404, 422 (no domain selected; unknown domain; a load URL off the nominated origins or on loopback/private address space; a non-safe method without `environment_disposable`; an unknown `$NAME`; more than `NONFUNCTIONAL_MAX_LOAD_PROFILES`; a second in-progress run for the same requirement; export requested with no tracker connected; finished sprint).

#### `GET /api/sprints/{sprint_id}/nonfunctional-runs`

List a sprint's nonfunctional runs, newest first.

#### `GET /api/nonfunctional-runs/{run_id}`

One run: its examined URLs with per-domain outcomes and measurements, its load profiles with what they actually sent, its findings, and the export roll-up.

#### `GET /api/nonfunctional-findings/{finding_id}/screenshot`

The page image captured when the URL was examined. 404 when the finding carries none — the normal case with `STORE_OFFLINE=false`.

#### `POST /api/nonfunctional-runs/{run_id}/restart`

Restart a failed run (uncapped). URLs are re-examined; load profiles that already sent traffic are not re-sent.

**Errors:** 404, 422 (not `failed`, outdated, finished sprint).

#### `POST /api/nonfunctional-runs/{run_id}/summarize`

Regenerate the best-effort run summary.

**Errors:** 404, 422 (run isn't `completed`), 502 (LLM failure — any existing summary is left untouched).

#### `POST /api/nonfunctional-runs/{run_id}/export-findings`

File this run's unfiled bug findings, on request — the manual half of the export rule, exactly as on the other two run modes.

**Errors:** 404, 422 (no tracker connected).

### Issue Tracker

A sprint can be connected to a **Jira project** or a **GitHub Issues repo** from the test-runs page, after which a run can file its **bug** findings there. `issue` findings are never filed: they say the testing was obstructed, not that the product is wrong, so there is nothing to report to a developer.

**One rule governs when filing happens automatically:** a run that **finished** reports its bugs; anything else waits for a human. Every abnormal ending — superseded by an upstream edit, retries exhausted, the sprint finished underneath it, a worker crash — leaves a finding set that is incomplete _and known to be incomplete_, and that is not written unasked into a tracker other people read. Those findings are not stranded: they stay on the run page with their cards, and the run's **File / Retry** button files them on request. That also keeps every outbound tracker call inside a worker or an explicit user action — the web process never files anything on a background sweep, and neither the reconciler nor finish-sprint touch a tracker.

**Findings are grouped before filing**, so one defect becomes one ticket rather than one ticket per failing test case. The grouping is not computed here — it is the sprint-wide one described under [Defect grouping](#defect-grouping) below, which runs when a run completes and again before anything is filed, so a run filed by hand after never completing is still grouped before an irreversible write. A deterministic prefilter collapses findings whose text is identical once normalized — the common case, since one broken dependency fails every case in a plan with the same words — and a single LLM call catches paraphrases the prefilter misses. Grouping is deliberately conservative: a defect wrongly split into two tickets costs a few minutes of triage, while one wrongly merged disappears. A defect remembers **one ticket per tracker** (`DefectGroupTicket`), so switching the sprint to another tracker files a fresh ticket there and switching back adopts the original rather than filing a third. The findings that were grouped in are listed on the ticket under _Also observed as_ with their run and timestamp, since nothing is ever appended to a ticket afterwards. Every finding keeps its own card in-app regardless.

**De-duplication is scoped to the sprint and to the currently connected tracker.** Before adopting an existing ticket the exporter checks it is still open; a closed ticket is a decision somebody made, so a new ticket is filed carrying a back-reference rather than the old one being silently reused.

Credentials are Fernet-encrypted at rest (`ENCRYPTION_KEY`), never serialized, and never logged. Every string sent to the tracker is redacted against the sprint's test-environment variable values first — base URLs excluded, since a bug report has to be allowed to name the page it is about. Exploratory findings attach their screenshot to a Jira issue when one exists (GitHub has no issue attachment API); a missing screenshot is normal under `STORE_OFFLINE=false` and never fails the filing.

#### `GET /api/sprints/{sprint_id}/issue-tracker`

The sprint's tracker connection, or `null` when there is none. The API token is never included in any response.

**Response** (200): `IssueTrackerConfigResponse` — `{ "id", "sprint_id", "provider", "target", "target_label", "base_url", "account_email", "issue_type", "verified_at", "created_at", "updated_at" }`, or `null`.

**Errors:** 404 (unknown sprint).

#### `PUT /api/sprints/{sprint_id}/issue-tracker`

Connect a tracker, or re-point an existing connection — provider switch included. The credentials are verified against the live tracker **inside the request** on every save, first connect and edit alike, and nothing is persisted unless that succeeds.

**Request:** `application/json`

| Field             | Jira                                                 | GitHub                  |
| ----------------- | ---------------------------------------------------- | ----------------------- |
| `provider`        | `"jira"`                                             | `"github"`              |
| `base_url`        | required — `https://your-team.atlassian.net`         | ignored (nulled)        |
| `account_email`   | required — the Atlassian account                     | ignored (nulled)        |
| `target`          | required — project key, e.g. `QA`                    | required — `owner/repo` |
| `issue_type`      | required — e.g. `Bug`, validated against the project | ignored (nulled)        |
| `api_token`       | API token                                            | personal access token   |
| `use_sprint_repo` | rejected (422)                                       | optional — see below    |

**`use_sprint_repo` (GitHub only)** files into the sprint's own registered repository: `target` may then be blank, since `owner/repo` is derived server-side from `Repo.github_link` rather than trusted from the request, and an omitted `api_token` falls back to the repo's stored access token. The token is **copied at save time** — decrypted, verified, re-encrypted into the tracker config — so the result is indistinguishable from a typed one and every export path is unchanged; rotating the repo's token later does not follow through, and the tracker is re-saved instead. A repo registered without a token is ordinary (public ones need none to read), so its absence simply falls through to the token rules below.

Token resolution, first match wins: a token in the request → the sprint repo's token (when `use_sprint_repo` is set) → the stored one on a same-provider edit → 422. Three edit rules follow from that:

- **The token may be omitted** when the provider is unchanged: blank or absent means "keep the stored one", which is decrypted and used for the verification call.
- **The token is required when the provider changes** — a Jira API token is meaningless to GitHub, so reusing it silently would verify nothing and store a credential that can never work (422). `use_sprint_repo` is the exception, and only because that token is GitHub's by construction rather than the previous tracker's.
- **Provider-irrelevant fields are cleared on a switch**, so a stale Jira site can never linger on a GitHub config.

Already-filed findings are untouched by any edit: their issue links still point where they were actually filed, and they are excluded from the new tracker's de-duplication window. That exclusion matters most on GitHub, whose issue numbers are per-repo integers — without it, repo B's `#7` would answer "is it still open?" for repo A's `#7` and a finding would be attached to an unrelated ticket.

A GitHub repo is accepted with a fine-grained token holding Issues:write and no push permission — requiring push would reject exactly the narrowly-scoped tokens worth creating for this.

**Response** (200): `IssueTrackerConfigResponse` (no token).

**Errors:** 404 (sprint); 422 (missing provider-specific field, malformed target, unknown provider, missing token on a first connect or a provider switch, `use_sprint_repo` with a non-GitHub provider or on a sprint whose repo link is not a GitHub URL, or the tracker refusing — bad credentials, unknown project/repo, unknown issue type, issues disabled); 500 (`ENCRYPTION_KEY` unset); 502 (the tracker could not be reached).

#### `DELETE /api/sprints/{sprint_id}/issue-tracker`

Disconnect the tracker. Deliberately **not** blocked by a run in flight — that run's export simply fails into `tracker_error` and its findings wait on the run page for a retry.

**Response** (204). **Errors:** 404 (sprint, or nothing connected).

#### `POST /api/test-runs/{run_id}/export-findings` and `POST /api/exploratory-runs/{run_id}/export-findings`

File this run's unfiled bug findings, on request — synchronous and uncapped, like `POST /api/exploratory-runs/{id}/summarize`. This is the manual half of the export rule rather than a fallback: a run that ended any way other than `completed` reaches its page with the bugs it _did_ find unfiled by design, and this is how they get filed. Retrying findings whose filing failed is the same operation. A run with nothing pending is a no-op that still returns the refreshed run.

**Response** (200): the run's detail shape, refreshed.

**Errors:** 404 (unknown run); 422 (no tracker connected).

#### Export state on a run

Both run shapes — list and detail, scripted and exploratory — carry an export roll-up, computed at response time and never stored:

| Field                      | Meaning                                                                                                                         |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `exported_finding_count`   | Bug findings that reached a ticket.                                                                                             |
| `exported_issue_count`     | Distinct tickets they became. Both totals are shown, so grouping reads as grouping rather than as findings having gone missing. |
| `export_groups`            | `[{ issue_key, issue_url, finding_count }]` — which findings became which ticket.                                               |
| `unexported_finding_count` | Bug findings with no ticket yet.                                                                                                |
| `export_error_count`       | A **subset** of the above: those whose filing was attempted and failed.                                                         |

Each finding additionally carries `tracker_issue_key`, `tracker_issue_url`, `tracker_error`, and `tracker_is_duplicate` on its `finding` object — all null/false on a finding that was never filed, which is a normal state rather than a failure.

Run creation on both run types accepts `"export_findings": true` to arm the automatic path; it 422s when set with no tracker connected. The flag is decided at run start and never after, so a tracker connected (or disconnected) later cannot retroactively change what a finished run was supposed to do.

### Defect grouping

No endpoint of its own — it runs inside the worker, and both the tracker export and the QA metrics panel read its result.

A sprint remembers its **distinct defects** as `DefectGroup` rows, one per defect, with every bug finding on either carrier pointing at one through a nullable `defect_group_id`. `services/finding_grouping.py` assigns them when a run completes, immediately before the export, and once more inside the export itself so a run filed by hand after never completing is grouped before an irreversible write. One rule: **grouping happens when a run completes, and again before anything is filed.**

This is what makes paraphrase-aware grouping available with **no issue tracker connected** — previously the LLM pass only ran on the way to filing, so a sprint without a tracker fell back to exact-text matching and reported the same defect several times.

Three properties are worth knowing:

- **Append-only.** A finding joins an existing group or opens a new one; nothing existing is ever rewritten, and a group's representative text is frozen at creation. That keeps the reported numbers stable between polls and keeps the model's match targets from drifting run to run.
- **`bug_count` is monotonic within a sprint.** Fixing a bug does not lower it: the failed row that observed the defect keeps its finding and its group, and that run still completed. The panel reports what the sprint's testing _found_, not what is currently open. A regression rejoins its original group rather than opening a second one.
- **A clean run costs nothing** — no LLM call and no query. The pass exits before reading the sprint's defects when the run produced no bug findings.

A defect remembers **one ticket per tracker** (`DefectGroupTicket`, unique on `(defect_group_id, tracker_target)`), so a tracker switch files a fresh ticket in the new tracker while the old one stays where it was, and switching back adopts the original rather than filing a third.

**Data egress note:** with no tracker connected, bug text now reaches the LLM provider where previously it did not. Not new exposure in practice — scripted findings were _written_ by `diagnose_and_fix_script` and exploratory ones by the `record_finding` tool, so the provider has already seen every word — but it is a real change in when the call happens, and worth stating rather than discovering.

### QA Metrics

### CI/CD export

A sprint's cached Playwright scripts become a **pull request** against the sprint's own
repository: the scripts committed verbatim under
`qa-agent-tests/<requirement>_<id>/<case>_<id>.py`, plus CI configuration an LLM authors after
reading the repository's existing conventions. Nothing merges it — the pull request is the
deliverable, and its review is what catches a generated workflow being wrong.

**Connecting.** One `CicdConfig` per sprint: a provider (GitHub Actions or Jenkins), a
Fernet-encrypted write token, and an optional free-text note about the CI environment. There is no
repository field — the destination is always the sprint's own registered repository, derived from
`Repo.github_link`. The token is verified against the live repository on **every** save; nothing
persists if that fails. A provider switch keeps the stored credential, unlike the issue tracker:
Jenkins also ships as a GitHub pull request, so the token is GitHub's either way.

Verification (`github_utils.check_write_access`) asks two questions in one request, because they
have two answers. `permissions.push` reports what the **account** may do to the repository;
`X-OAuth-Scopes` reports what the **credential** was granted. A classic token scoped `repo` but not
`workflow` passes the first and fails the second — and since a GitHub Actions export always commits
a file under `.github/workflows/`, which GitHub gates behind that separate scope and refuses with a
bare `404`, the save is refused for the Actions provider when the scope is known to be absent.
Fine-grained tokens send no scope header; they are accepted (unknown is not missing) and covered by
`create_tree`, which re-maps a 404 on a workflow path to a message naming the scope instead of
"repository not found". Jenkins needs neither grant — it writes a Jenkinsfile.

**Eligibility.** A case can be exported when it has a cached script and the three
`script_*_revision` values stamped when that script was cached still match the sprint's current
requirement, plan and environment. The comparison is `outdated_reasons` — the same one the run
badges use. NULL revisions (a script cached before stamping existed) read as stale. Ineligible
cases are returned with their reason rather than filtered out, because "never run" and "out of
date" imply different actions.

**The job.** Refresh repo metadata and file tree → re-derive eligibility from the database → fetch
the existing CI files → build the deterministic install/run block → **one** LLM call with a
bounded `read_file` loop → validate → splice → write. The write sequence is four requests
regardless of case count, over one shared client: tree (content inline) → commit → ref → PR.

**The pull-request text.** The title is the model's, verbatim. The body is the model's prose plus
a deterministic trailer (`cicd_export.pr_body`) below a horizontal rule: the sprint, an inventory
of every committed case grouped under its requirement with the path its script landed at, the
variables and secrets the team must create before the job runs (**names only**), any generation
notes, and any path the gate dropped. The prompt tells the model that the trailer exists and
forbids restating it — two setup checklists, one written from the names the model was given and one
from what was actually committed, leave a reviewer no way to choose when they disagree. The model's
half is meant to carry what only it knows: how the suite was wired in, which conventions it
followed, and every deviation with its reason. The commit message is a plain f-string, not
generated.

**What the model may and may not author.** It authors CI files and the pull-request prose. It
authors a **job or stage body** for an existing file — never the file itself, so a truncating
rewrite of the team's CI is not expressible; the splice is ours (`add_job` / `insert_stage`).
There is no field that could carry a test script, `qa-agent-tests/` is outside the path allowlist,
and the commit applies our scripts last — three layers, because the schema alone did not stop a
`files` entry aimed at a script's own path. It is shown environment variable and secret **names**,
never values.

**Variable and secret names.** Which variables become plain CI variables and which become secrets
is decided by `environment_utils.variable_and_secret_names`: an `http(s)` value counts as a plain
variable only when it carries no userinfo and no query string, so a basic-auth URL or a webhook
with a token in it goes to the secret store. The CI-side name itself comes from
`cicd_export.reference_map` — a single derivation shared by the deterministic block, the prompt,
the pull-request trailer and the gate, since a name they each derived separately is a name they can
disagree about. `base_url` becomes `BASE_URL`, and Actions reserves the `GITHUB_` prefix, so
`GITHUB_PAT` becomes `QA_GITHUB_PAT`; the eligibility endpoint and the trailer both report the
CI-side name with the sprint's own alongside it. Sanitizing is many-to-one, so two sprint names
that map alike (`base_url` and `base.url`) are separated by a numeric suffix — within a namespace
only, since `vars.X` and `secrets.X` are different stores and not the same name. The assignment
sorts by name rather than following `env_vars_json`, whose order an edit can change. Both providers
bind the same direction — CI supplies the mapped name and the script reads the sprint's own name
from `os.environ`.

**The gate** (`cicd_export.validate`), run before any write: a path allowlist (the one place model
output becomes a filesystem effect), a host-edit target check (only a file this export actually
fetched, and only one whose triggers already fit — a job inherits its workflow's triggers, so this
is what keeps an environment-dependent suite off every pull request), a structural floor, and
reference resolution (every `vars`/`secrets`/`credentials` name must be one we supplied). Jenkins'
own `env` names — `BUILD_NUMBER`, `WORKSPACE`, `BRANCH_NAME` and the rest — are allowed through
that last check, since Jenkins supplies them and plugins extend the set; Actions' `vars`/`secrets`
namespaces are closed and need no such allowance. A failure raises, which costs a retry rather than
a review cycle.

**Endpoints.**

| Method   | Path                                 | Purpose                                                                         |
| -------- | ------------------------------------ | ------------------------------------------------------------------------------- |
| `GET`    | `/api/sprints/{id}/cicd-config`      | The connection, or `null`. Never the token                                      |
| `PUT`    | `/api/sprints/{id}/cicd-config`      | Connect or edit; verifies push permission first (422 / 502)                     |
| `DELETE` | `/api/sprints/{id}/cicd-config`      | Disconnect (204; 404 when absent)                                               |
| `GET`    | `/api/sprints/{id}/cicd-eligibility` | Every case with its export eligibility, plus the variable/secret **name** split |
| `POST`   | `/api/sprints/{id}/cicd-exports`     | Start an export of the selected cases (201)                                     |
| `GET`    | `/api/sprints/{id}/cicd-exports`     | Export history, newest first                                                    |
| `GET`    | `/api/cicd-exports/{id}`             | One export, for polling                                                         |
| `POST`   | `/api/cicd-exports/{id}/restart`     | Retry a failed export; refused while `running`                                  |

`POST .../cicd-exports` refuses **before** creating a row when no config exists, when the sprint
has no test-environment variables (the generated job would have nothing to run against), or when
no selected case is eligible — so a request that cannot succeed never costs an LLM call and never
leaves a `failed` row to explain.

**Receipts.** `CicdExportItem` rows are written only after the commit succeeds — an export that
failed part-way claims nothing. What the user selected lives separately in
`selected_case_ids_json`. Restart is uncapped and writes a fresh branch each attempt, which is
what makes a retry idempotent. A finished sprint may still export.

#### `GET /api/sprints/{sprint_id}/qa-metrics`

How QA went for one sprint. A pure read — no LLM call, no write, nothing stored — computed from the rows that already exist, so it is safe for the test-runs page to poll on the same 2.5 s interval as the run lists. 404 if the sprint is unknown.

**Only `completed` runs are counted**, mirroring the export rule: an aborted or in-flight run's finding set is incomplete _and known to be incomplete_, and its case denominator under-counts because the cases it never reached never ran. Excluded runs are counted and named rather than silently dropped.

| Field                                                            | Meaning                                                                                                                                                                                                                                                                                                                                                         |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `distinct_test_cases_run`                                        | Distinct test cases with at least one terminal execution in a counted run. **The density denominator.**                                                                                                                                                                                                                                                         |
| `case_executions`                                                | How many times a case ran. A case executed three times adds 1 to the count above and 3 to this one.                                                                                                                                                                                                                                                             |
| `executions_passed` / `executions_failed` / `executions_errored` | The execution split, one status each.                                                                                                                                                                                                                                                                                                                           |
| `exploratory_sessions` / `requirements_explored`                 | Counted separately and **never summed** with the scripted counts — a 25-action browser session and a 3-step script are not the same unit.                                                                                                                                                                                                                       |
| `bug_count`                                                      | **Distinct defects**, not findings. See the collapse rule below.                                                                                                                                                                                                                                                                                                |
| `issue_count`                                                    | Issue findings, counted **raw** — issues are never collapsed. See the collapse rule below.                                                                                                                                                                                                                                                                      |
| `high_severity_bug_count`                                        | Defects where any member reported `high` — the highest severity in the group, so one cannot hide behind a medium duplicate.                                                                                                                                                                                                                                     |
| `requirements_covered` / `requirements_total`                    | Distinct requirements touched by a counted run (either mode), and the sprint's confirmed requirements beside it so coverage stays legible. `covered` can exceed `total` — coverage is what runs already did, the total is a live snapshot, so a covered requirement later edited or deleted leaves the two crossed. Reported side by side, never as a fraction. |
| `bugs_per_requirement` / `bugs_per_test_case`                    | Both `null` when their denominator is zero. Divided by `requirements_covered` and `distinct_test_cases_run` respectively — never by the sprint total, never by execution count.                                                                                                                                                                                 |
| `per_requirement`                                                | The breakdown, worst first. One row per **covered** requirement, findings or not; archived requirements included and flagged `requirement_deleted`.                                                                                                                                                                                                             |
| `excluded_runs_running` / `excluded_runs_failed`                 | Runs left out and why.                                                                                                                                                                                                                                                                                                                                          |

**Two counting levels for scripted cases, deliberately never reconciled.** `bugs_per_test_case` divides by distinct cases rather than by executions because otherwise re-running an unfixed plan three times makes the sprint read three times healthier — a metric that rewards noise. Keeping the levels separate is also what removes any need for a "what status does that case have?" tiebreak: each execution contributes its own single status, and the distinct count never asks.

**One bug is one defect.** Bug findings collapse three ways, in order: by the `DefectGroup` assigned when the run completed, then by `(tracker_target, tracker_issue_key)` where a ticket was filed — the pair, never the bare key, since GitHub issue numbers are per-repo integers — then by normalized text, reusing `finding_dedup.dedup_key` so the panel and the tracker cannot report different groupings of the same findings. The stored group outranks ticket identity: a defect found either side of a tracker switch is one bug, not two. Paraphrase grouping therefore works with **no tracker connected**, and still costs this endpoint nothing — the judgement happened at run completion, and `defect_group_id` is a plain column on rows the endpoint already loads.

**Issues are never collapsed.** An issue records that testing was obstructed, not that the product is wrong — the SBTM distinction — so "how many distinct defects" is not a question it answers. Three cases erroring on the same unreachable environment are three pieces of testing that did not happen, and collapsing them to one would understate how much of the run was lost. It also keeps `issue_count` in the same units as `executions_errored` beside it, which is the scripted half of the same figure.

**Per-requirement bug rows may sum above the headline.** Grouping is sprint-scoped, so one broken dependency can break login _and_ checkout: the defect counts once overall and once per requirement it touches. The frontend footnotes this only when the two actually differ. Issue rows cannot diverge this way — ungrouped findings each belong to exactly one requirement, so they sum to the headline exactly.

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
  migrations.py        # Idempotent startup migrations (run after init_db; list currently empty)
  worker.py            # RQ worker CLI (python -m backend.worker)
  models/
    database.py        # Table models: Repo, Sprint, Requirement, TestEnvironmentAccess, TestPlan, TestCase, IssueTrackerConfig, DefectGroup, DefectGroupTicket, TestRun, TestExecution, TestCaseExecution, ExploratoryRun, ExploratorySession, ExploratoryFinding
    types.py           # Request/response types
  routes/
    _common.py         # get_sprint_or_404 / ensure_sprint_active, plus the preconditions the two browser-driven run modes share
    auth.py            # POST /api/auth/verify, GET /api/auth/check
    repos.py           # Repo registration, listing, deactivation, README status
    sprints.py         # Sprint create/list/get/finish + QA metrics
    cicd.py            # CI/CD connection, per-case eligibility, exports, restart
    requirements.py    # Requirement CRUD, PRD upload/split + clarification/confirm/restart
    test_environment.py # Test environment get/submit/answer/confirm (synchronous LLM check) + env-var extraction/edit
    test_plans.py      # Test plan generate/list/feedback/edit/approve/restart
    test_execution.py  # Test run create/list/detail, script download, restart, export retry
    exploratory.py     # Charter drafting, exploratory run create/list/detail, session sheet, screenshot, restart, summary, export retry
    nonfunctional.py   # Run setup pair, nonfunctional run create/list/detail, screenshot, restart, summary, export retry
    issue_tracker.py   # Issue-tracker config get/save/delete (live verification on every save)
  services/
    storage.py         # Conditional README/PRD/screenshot persistence (STORE_OFFLINE)
    queue.py           # RQ queue service (graceful degradation when Redis is down)
    llm.py             # OpenAI-SDK client: clarity/test-env checks, env-var extraction, PRD split, test plans (code-blind), test-script tool loop
    llm_prompts.py      # System prompts, prompt-assembly helpers, TestCaseLike, read_file schema, the two browser tool schemas
    script_runner.py    # Subprocess execution of generated test scripts (no sandboxing beyond a timeout)
    browser_session.py  # One Playwright browser: tool executors, the catalogue checks, endpoint capture, the arrival hook (DB-free)
    issue_tracker.py    # Jira/GitHub transport: verify, create issue, state check, screenshot attach, redaction
    findings.py         # RUN_KINDS — the one run-type dispatch — plus one walk over a parent's findings and one definition of what counts
    finding_dedup.py    # Grouping mechanics: deterministic prefilter + one LLM pass; never raises
    finding_grouping.py # Assigns a run's bug findings to the sprint's DefectGroup rows; never raises
    finding_export.py   # Which findings to file, and writing the receipts back; never raises
    qa_metrics.py       # Per-sprint QA metrics, computed at response time; never raises
    nonfunctional_checks.py # The nonfunctional oracle: axe parsing, the passive-security table, run-scoped dedup; pure
    load_runner.py      # One approved load profile over a thread pool, two ceiling tiers, an SSRF refusal; never raises
    cicd_eligibility.py # Which cached scripts still describe the sprint
    cicd_export.py      # Script layout, the deterministic CI block, the validation gate
    ci_introspect.py    # Read existing GitHub Actions workflows into facts; splice a job
    jenkins_text.py     # Brace scanner, structural floor, stage splice
    invalidation.py     # What editing a confirmed artifact invalidates
    finalization.py     # A terminal parent leaves no non-terminal children; the shared retry protocol (RowSpec)
    reconciler.py      # Re-enqueues lost jobs, sweeps crashed-worker heartbeats (six row types); SWEEP_SPECS also drives finish_sprint
  tasks/
    analyze_requirement.py  # The analysis task executed by the worker
    generate_test_plan.py   # The plan-generation task (single LLM call, no repo access)
    execute_test.py         # The test-execution task (per-case self-heal loop)
    explore_requirement.py  # The exploratory task (per-charter browser tool loop)
    run_nonfunctional.py    # The nonfunctional task (walk, catalogue on arrival, endpoints, load profiles, triage)
  scripts/
    clear_queue.py     # Queue maintenance CLI (python -m backend.scripts.clear_queue)
    reset_db.py        # Drop + recreate all tables (python -m backend.scripts.reset_db)
  utils/
    auth.py            # verify_auth cookie dependency
    crypto.py          # Fernet encryption for GitHub tokens
    github_utils.py    # GitHub API client and error hierarchy
    prd_utils.py       # PRD text extraction (.md/.txt via UTF-8, .pdf via pypdf, .docx via python-docx)
    http_utils.py      # SSL_CONTEXT — the certifi context every outbound HTTPS client must use
    readme_utils.py    # Best-effort README resolution (stored copy → re-download → none) + refresh_project_context for a run
    sprint_utils.py    # Unique sprint directory generation
    upload_utils.py    # read_upload_capped — bounded multipart reads
    environment_utils.py # Where a finding was observed; which env values may leave, and as what
    exploratory_utils.py # session_sheets(run) — session rows as plain prompt data
    nonfunctional_utils.py # target/profile rows as plain prompt data; tolerant JSON-blob decoding
  tests/               # pytest suite (in-memory SQLite, mocked GitHub API, Redis + LLM stubbed)
```

## Requirements

- Python 3.10+
- PostgreSQL
- Redis (for requirement analysis, test-plan generation, test execution, and exploratory testing; optional otherwise)
- An LLM API key (`OPENAI_API_KEY` — for requirement analysis, the test-environment check, test-plan generation, test execution, and exploratory testing)
- For test execution and exploratory testing: `playwright install chromium` on the worker host (one-time, after `pip install -e .` — `playwright` is a base dependency, so the `[dev]` extra isn't needed for this)
- Dependencies declared in `pyproject.toml` (install with `pip install -e ".[dev]"` from the repo root; `pip install -e .` alone covers everything except the test/lint tooling)
- If using conda, start `python -m backend.worker` from an **activated** environment — generated test scripts inherit the worker process's own environment as-is, so an unactivated worker means an unactivated (and potentially broken) script environment too
