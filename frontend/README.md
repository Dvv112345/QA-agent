# QA Agent Frontend

React + TypeScript + Vite frontend for the QA Agent.

## Quick Start

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

## Environment Variables

| Variable        | Default                 | Description          |
| --------------- | ----------------------- | -------------------- |
| `VITE_API_BASE` | `http://localhost:8000` | Backend API base URL |

Define in `frontend/.env` (see `.env.example`).

## Login Flow

When `APP_PASSWORD` is set on the backend, the frontend shows a full-screen login modal:

1. On page load, `GET /api/auth/check` checks whether a valid `qa_auth` cookie already exists.
2. If `valid: true` — the modal is skipped, the app appears immediately.
3. If `valid: false` — a password input is shown.
4. On submit, `POST /api/auth/verify` validates the password. On success the backend sets an HttpOnly session cookie; on failure an inline error is displayed.
5. The cookie is sent automatically by the browser on every subsequent API request — no changes to `uploadFiles()` or `fetchJobStatus()` are needed.

If `APP_PASSWORD` is not set on the backend, all auth checks return `{ valid: true }` and the modal never appears.

## Scripts

| Command              | Purpose                       |
| -------------------- | ----------------------------- |
| `npm run dev`        | Start Vite dev server         |
| `npm run build`      | Type-check + production build |
| `npm run lint`       | ESLint                        |
| `npm test`           | Vitest                        |
| `npm run test:watch` | Vitest in watch mode          |
| `npm run preview`    | Preview production build      |
