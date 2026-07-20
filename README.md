# QA Agent

Automated Quality Assurance analysis for source code and requirement documents. Register GitHub repositories, create sprints against them, and enter sprint requirements manually or by uploading a PRD document (`.md`, `.txt`, `.pdf`, `.docx`) that an LLM splits into requirements. An LLM checks each requirement for QA-clarity through a clarification loop; then describe and validate test environment access to lock the requirement set, and generate a reviewable test plan per requirement — the LLM reads repository files to ground test cases in the real code, and each plan goes through a feedback/edit loop until approved.

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 22+
- PostgreSQL with a `qa_agent` database (`createdb qa_agent`)
- Redis (optional — required for requirement analysis and test-plan generation)
- An LLM API key (`OPENAI_API_KEY`, any OpenAI-compatible provider — required for requirement analysis, the test-environment check, and test-plan generation)

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

### Worker (requires Redis)

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
