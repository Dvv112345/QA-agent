# QA Agent

Automated Quality Assurance analysis for source code and requirement documents.

Register GitHub repositories, create sprints against them, and enter sprint requirements manually or by uploading a PRD document (`.md`, `.txt`, `.pdf`, `.docx`) that an LLM splits into requirements. An LLM checks each requirement for QA-clarity through a clarification loop.

Then describe and validate test environment access — the LLM also extracts the access details into editable environment variables — and generate a reviewable test plan per requirement, grounded in the requirement itself rather than the implementation, written for automated execution, and taken through a feedback/edit loop until approved.

Finally, run the approved test plans: the LLM writes (or reuses) a Playwright script per test case, reading the repository to get real endpoints and response shapes right, executes it against the confirmed environment, and self-heals script bugs automatically. Genuine application bugs are reported as structured findings — severity, reproduction steps, expected vs actual, and the environment they were seen in — the same shape exploratory testing produces.

The same gate opens a second run mode, **exploratory testing**: instead of executing predefined cases, an LLM drives a real browser against the running application to look for what the test plan didn't anticipate. It is organized with SBTM — a run covers exactly one requirement, split into time-boxed sessions that each pursue a single charter and produce a reviewable session sheet — and the charters are drafted from the SFDIPOT heuristic so they attack the requirement from genuinely different angles, then edited by you before the run starts. Sessions record findings live, each with a screenshot and the browser, viewport and page URL it was seen in. Navigation is locked to the application URLs you nominate, and secrets are typed through a tool that resolves the value outside the LLM's transcript.

Every confirmed artifact stays editable: correcting a requirement removes its test plan and sends the environment back for re-checking, changing the environment removes the sprint's plans, and editing an approved plan returns it to draft. Test runs that already executed are never deleted — they are kept and marked as out of date, naming which artifact moved.

Each sprint's test-runs page opens with a **QA metrics panel** — how much was tested (distinct test cases and exploratory sessions, counted separately), how many _distinct_ defects came out of it, defect density per requirement and per test case, and which features are carrying the bugs. The defect count is **paraphrase-aware and works with no issue tracker connected**: when a run finishes, an LLM decides which of its bug findings are further occurrences of defects the sprint already knows about, and the answer is remembered — so re-running an unfixed plan does not inflate the count, and two differently-worded reports of one defect are one bug.

A sprint can also be connected to a **Jira project or GitHub Issues repo** — for GitHub, one checkbox files into the sprint's own registered repository using its stored access token — and a run can file its bug findings there: findings are grouped first, so one defect becomes one ticket rather than one per failing test case, and only a run that finished reports automatically — anything else offers a button instead.

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 22+
- PostgreSQL with a `qa_agent` database (`createdb qa_agent`)
- Redis (optional — required for requirement analysis, test-plan generation, test execution, and exploratory testing)
- An LLM API key (`OPENAI_API_KEY`, any OpenAI-compatible provider — required for requirement analysis, the test-environment check, test-plan generation, test execution, and exploratory testing)
- `ENCRYPTION_KEY` (a Fernet key — required to register a repo with an access token, and to connect an issue tracker)
- Optional: a Jira or GitHub Issues account, to file bug findings from a run (for GitHub, the sprint's own repository and its stored token can be used instead — the token needs permission to write issues)
- For test execution and exploratory testing: `playwright install chromium` on the worker host (one-time, after `pip install -e ".[dev]"`)

### Backend

```bash
# Install dependencies (from the repo root)
pip install -e ".[dev]"

# Configure environment
cp backend/.env.example backend/.env

# Start the API server (http://localhost:8000)
python -m backend.main
```

### Frontend

```bash
cd frontend

# Install dependencies
npm install

# Start the dev server (http://localhost:5173)
npm run dev
```

### Worker (requires Redis; `playwright install chromium` for test execution and exploratory testing)

```bash
# Start Redis, then in a separate terminal:
python -m backend.worker

# Clear all jobs from the queue
python -m backend.scripts.clear_queue

# Drop and recreate all database tables (dev utility)
python -m backend.scripts.reset_db
```

## Project Structure

| Directory   | Description                         |
| ----------- | ----------------------------------- |
| `backend/`  | FastAPI server, RQ worker, services |
| `frontend/` | React + Vite + TypeScript SPA       |
| `.github/`  | CI workflows (backend + frontend)   |

## Documentation

- Backend: [`backend/README.md`](backend/README.md)
- Frontend: [`frontend/README.md`](frontend/README.md)
- Development guide: [`CLAUDE.md`](CLAUDE.md)

## Testing

```bash
# Backend tests
pip install -e ".[dev]"
python -m pytest -v

# Frontend tests
cd frontend && npm test
```
