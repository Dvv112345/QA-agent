"""Conditional file storage for sprint README files.

When ``STORE_OFFLINE`` is ``True``, README files are persisted to
``STORAGE_LOCATION/<directory>/README.md`` on disk.  Otherwise writes
are silently skipped.
"""

import logging
import os

from backend.config import STORAGE_LOCATION, STORE_OFFLINE

logger = logging.getLogger(__name__)


class StorageService:
    """Handles conditional persistence of sprint README files."""

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

    def store_readme(self, md_bytes: bytes, directory: str) -> str | None:
        """Persist a README file to disk if offline mode is active.

        Returns the absolute path to the saved file, or ``None`` when
        ``STORE_OFFLINE`` is disabled.
        """
        if not self._offline:
            return None

        sprint_dir = os.path.join(self._base, directory)
        os.makedirs(sprint_dir, exist_ok=True)

        readme_path = os.path.join(sprint_dir, "README.md")
        with open(readme_path, "w", encoding="utf-8") as fh:
            fh.write(md_bytes.decode("utf-8", errors="replace"))

        logger.info("Stored README → %s", readme_path)
        return os.path.abspath(readme_path)
