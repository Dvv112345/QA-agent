from sqlmodel import SQLModel


class HealthResponse(SQLModel):
    status: str
    storage: str = "unknown"
    redis: str = "unknown"


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
