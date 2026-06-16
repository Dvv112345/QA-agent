from typing import Optional

from sqlmodel import SQLModel


class HealthResponse(SQLModel):
    status: str


class UploadResponse(SQLModel):
    job_id: str
    status: str
    zip_filename: str
    markdown_filename: str
    tree: list[str]
    tree_text: str
    stored_path: Optional[str] = None
    error: Optional[str] = None
