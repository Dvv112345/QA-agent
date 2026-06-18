import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, UploadFile

from backend.config import (
    CHUNK_SIZE,
    MAX_TREE_DEPTH,
    MAX_UPLOAD_SIZE_MB,
    MAX_ZIP_FILES,
    STORE_OFFLINE,
)
from backend.models.types import UploadResponse
from backend.services.queue import get_queue_service
from backend.services.storage import StorageService
from backend.utils.zip_utils import extract_and_list_tree

router = APIRouter()

storage_service = StorageService()

logger = logging.getLogger(__name__)

ALLOWED_ZIP_EXTENSIONS = {".zip"}
ALLOWED_MARKDOWN_EXTENSIONS = {".md", ".markdown"}
ZIP_MAGIC = b"PK\x03\x04"


def _validate_extension(filename: str, allowed: set[str], label: str) -> None:
    """Raise 422 if the filename does not end with an allowed extension."""
    lower = filename.lower()
    if not any(lower.endswith(ext) for ext in allowed):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid {label} file: '{filename}'. "
                f"Expected extension: {', '.join(sorted(allowed))}"
            ),
        )


def _validate_zip_content(data: bytes, filename: str) -> None:
    """Raise 422 if *data* does not start with the ZIP magic bytes."""
    if not data.startswith(ZIP_MAGIC):
        raise HTTPException(
            status_code=422,
            detail=(f"Invalid zip file: '{filename}' does not appear to be a valid ZIP archive."),
        )


def _validate_markdown_content(data: bytes, filename: str) -> None:
    """Raise 422 if *data* cannot be decoded as UTF-8 text."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=422,
            detail=(f"Invalid markdown file: '{filename}' is not valid UTF-8 text."),
        ) from e


def _generate_job_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex
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

    # Check upload size
    max_bytes = MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if zip_file.size > max_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"Zip file exceeds the maximum allowed size of {MAX_UPLOAD_SIZE_MB} MB.",
        )
    if markdown_file.size > max_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"Markdown file exceeds the maximum allowed size of {MAX_UPLOAD_SIZE_MB} MB.",
        )

    # Read file contents into memory
    zip_bytes = await zip_file.read()
    md_bytes = await markdown_file.read()

    # Validate actual content (not just file extension)
    _validate_zip_content(zip_bytes, zip_file.filename)
    _validate_markdown_content(md_bytes, markdown_file.filename)

    job_id = _generate_job_id()

    # Persist files if offline mode is enabled
    storage_result = storage_service.store(zip_bytes, md_bytes, job_id)
    zip_path = storage_result.get("zip_path")

    # Extract zip and build directory tree
    try:
        tree, tree_text, files = extract_and_list_tree(
            zip_bytes,
            stored_path=zip_path,
            max_files=MAX_ZIP_FILES,
            max_depth=MAX_TREE_DEPTH,
            chunk_size=CHUNK_SIZE,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.warning(str(e))
        raise HTTPException(
            status_code=422,
            detail=(
                "Failed to process the uploaded zip archive. "
                "It may be corrupt or use an unsupported format."
            ),
        ) from e

    # Enqueue word-count job when offline storage is enabled and files were persisted
    word_count_enqueued = False
    if STORE_OFFLINE and storage_result.get("stored"):
        get_queue_service().enqueue_word_count(
            job_id,
            storage_result["md_path"],
            storage_result["zip_path"],
            files,
        )
        word_count_enqueued = True

    return UploadResponse(
        job_id=job_id,
        status="received",
        zip_filename=zip_file.filename,
        markdown_filename=markdown_file.filename,
        tree=tree,
        tree_text=tree_text,
        word_count_enqueued=word_count_enqueued,
        error=None,
    )
