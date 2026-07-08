from datetime import datetime

from sqlmodel import SQLModel

# ── Health ────────────────────────────────────────────────────────────


class HealthResponse(SQLModel):
    status: str
    storage: str = "unknown"
    redis: str = "unknown"


# ── Auth (preserved) ──────────────────────────────────────────────────


class PasswordVerifyRequest(SQLModel):
    password: str


class AuthCheckResponse(SQLModel):
    valid: bool


# ── Upload / Word-count (preserved) ──────────────────────────────────


class FileWordCount(SQLModel):
    file: str
    words: int


class JobStatusResponse(SQLModel):
    job_id: str
    status: str  # "queued" | "started" | "finished" | "failed" | "unknown"
    total_files: int = 0
    processed_files: int = 0
    md_result: FileWordCount | None = None
    zip_results: list[FileWordCount] | None = None
    total_words: int | None = None
    error: str | None = None


class UploadResponse(SQLModel):
    job_id: str
    status: str
    zip_filename: str
    markdown_filename: str
    tree: list[str]
    tree_text: str
    word_count_enqueued: bool = False
    error: str | None = None


# ── Repo ──────────────────────────────────────────────────────────────


class RepoResponse(SQLModel):
    id: int
    github_link: str
    name: str
    description: str | None = None
    active: bool
    created_at: datetime


class ReadmeStatusResponse(SQLModel):
    has_readme: bool


# ── Sprint ────────────────────────────────────────────────────────────


class SprintResponse(SQLModel):
    id: int
    name: str
    repo_id: int
    active: bool
    directory: str
    created_at: datetime
    repo: RepoResponse | None = None


class SprintCreateRequest(SQLModel):
    name: str
    repo_id: int


class SprintUpdateRequest(SQLModel):
    active: bool
