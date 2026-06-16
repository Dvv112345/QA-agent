import logging
import os

from backend.config import (
    CHUNK_SIZE,
    MAX_TREE_DEPTH,
    MAX_ZIP_FILES,
    STORAGE_LOCATION,
    STORE_OFFLINE,
)
from backend.utils.zip_utils import extract_zip

logger = logging.getLogger(__name__)


class StorageService:
    """Handles conditional persistence of uploaded files.

    When ``STORE_OFFLINE`` is ``True``, files are written to
    ``STORAGE_LOCATION/<job_id>/`` on disk.  Otherwise files are kept
    in memory only and ``store()`` is a no-op.
    """

    def __init__(self) -> None:
        self._offline = STORE_OFFLINE
        self._base = STORAGE_LOCATION

        if self._offline:
            if not self._base:
                raise RuntimeError(
                    "STORE_OFFLINE is set to 'true' but STORAGE_LOCATION is not configured."
                )
            try:
                os.makedirs(self._base, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    f"STORE_OFFLINE is 'true' but cannot create or write to "
                    f"STORAGE_LOCATION '{self._base}': {exc}"
                ) from exc

    @property
    def offline(self) -> bool:
        return self._offline

    def store(self, zip_bytes: bytes, md_bytes: bytes, job_id: str) -> dict:
        """Persist files to disk if offline mode is active.

        Returns a dict with storage metadata suitable for merging into
        the ``UploadResponse``.
        """
        if not self._offline:
            return {"stored": False}

        job_dir = os.path.join(self._base, job_id)
        os.makedirs(job_dir, exist_ok=True)

        zip_path = os.path.join(job_dir, "zip_source")
        md_path = os.path.join(job_dir, "requirements.md")
        extract_zip(zip_bytes, zip_path, MAX_ZIP_FILES, MAX_TREE_DEPTH, CHUNK_SIZE)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(md_bytes.decode("utf-8", errors="replace"))

        logger.info("Stored upload %s → %s", job_id, job_dir)

        return {"stored": True, "stored_path": job_dir, "zip_path": zip_path, "md_path": md_path}
