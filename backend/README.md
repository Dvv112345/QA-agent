# QA Agent Backend

FastAPI backend for the QA Agent — accepts source code (zip) and requirement documents (markdown) for analysis.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server
python -m backend.main
```

The API is served at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

## Environment Variables

| Variable           | Default             | Description                                                                                           |
| ------------------ | ------------------- | ----------------------------------------------------------------------------------------------------- |
| `STORE_OFFLINE`    | _(unset / `false`)_ | Set to `"true"` to persist uploaded files to disk. Any other value keeps files in memory only.        |
| `STORAGE_LOCATION` | _(unset)_           | Directory where files are stored when `STORE_OFFLINE=true`. Must be set when offline mode is enabled. |

Copy `.env.example` to `.env` and adjust:

```bash
cp .env.example .env
# Edit .env for your environment
```

## API Endpoints

### `GET /api/health`

Health check. Returns:

```json
{ "status": "ok" }
```

### `POST /api/upload`

Upload a zip archive and a markdown requirements file.

**Request:** `multipart/form-data`

| Field           | Required | Description                                |
| --------------- | -------- | ------------------------------------------ |
| `zip_file`      | Yes      | Source code archive (`.zip`)               |
| `markdown_file` | Yes      | Requirements document (`.md`, `.markdown`) |

**Response** (200):

```json
{
  "job_id": "20260616-143022-a1b2c3",
  "status": "received",
  "zip_filename": "source.zip",
  "markdown_filename": "requirements.md",
  "tree": ["src/", "src/main.py", "docs/readme.md"],
  "tree_text": "+-- src\n|   \\-- main.py\n\\-- docs\n    \\-- readme.md",
  "stored_path": null,
  "error": null
}
```

**Errors:**

| Status | Cause                                                   |
| ------ | ------------------------------------------------------- |
| 422    | Invalid file extension, missing file, or empty filename |
| 500    | Unexpected server error                                 |

### Example with curl

```bash
curl -X POST http://localhost:8000/api/upload \
  -F "zip_file=@source.zip" \
  -F "markdown_file=@requirements.md"
```

## Project Structure

```
backend/
  main.py              # App entry point, CORS, exception handlers
  config.py            # Environment variable configuration
  models/              # SQLModel type definitions
    upload.py          # UploadResponse, HealthResponse
  routes/              # API route handlers
    upload.py          # POST /api/upload
  services/            # Business logic
    storage.py         # Conditional file persistence
  utils/               # Utility functions
    zip_utils.py       # Zip extraction and directory tree
```

## Requirements

- Python 3.10+
- Dependencies listed in `requirements.txt`
