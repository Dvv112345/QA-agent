from sqlmodel import SQLModel


class HealthResponse(SQLModel):
    status: str
    storage: str = "unknown"


class UploadResponse(SQLModel):
    job_id: str
    status: str
    zip_filename: str
    markdown_filename: str
    tree: list[str]
    tree_text: str
    error: str | None = None
