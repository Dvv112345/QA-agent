import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, UploadFile

from backend.models.upload import UploadResponse
from backend.utils.zip_utils import extract_and_list_tree

router = APIRouter()

ALLOWED_ZIP_EXTENSIONS = {".zip"}
ALLOWED_MARKDOWN_EXTENSIONS = {".md", ".markdown"}


def _validate_extension(filename: str, allowed: set[str], label: str) -> None:
    """Raise 422 if the filename does not end with an allowed extension."""
    lower = filename.lower()
    if not any(lower.endswith(ext) for ext in allowed):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {label} file: '{filename}'. Expected extension: {', '.join(sorted(allowed))}",
        )


def _generate_job_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{ts}-{suffix}"


@router.post("/upload", response_model=UploadResponse)
async def upload_files(
    zip_file: UploadFile = File(...),
    markdown_file: UploadFile = File(...),
):
    # Check filenames exist
    if not zip_file.filename:
        raise HTTPException(status_code=422, detail="Zip file must have a filename.")
    if not markdown_file.filename:
        raise HTTPException(status_code=422, detail="Markdown file must have a filename.")

    # Validate extensions
    _validate_extension(zip_file.filename, ALLOWED_ZIP_EXTENSIONS, "zip")
    _validate_extension(markdown_file.filename, ALLOWED_MARKDOWN_EXTENSIONS, "markdown")

    # Read file contents into memory
    zip_bytes = await zip_file.read()
    md_bytes = await markdown_file.read()

    job_id = _generate_job_id()

    # Extract zip and build directory tree
    tree, tree_text = extract_and_list_tree(zip_bytes)

    # Storage is handled in a later phase.
    return UploadResponse(
        job_id=job_id,
        status="received",
        zip_filename=zip_file.filename,
        markdown_filename=markdown_file.filename,
        tree=tree,
        tree_text=tree_text,
        stored_path=None,
        error=None,
    )
