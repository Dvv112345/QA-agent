# QA Agent

Automated Quality Assurance analysis for source code and requirement documents. 

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 22+
- Redis (optional — required for word-count processing)

### Backend

```bash
# Install dependencies
pip install -r backend/requirements.txt

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
python -m backend.clear_queue
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
