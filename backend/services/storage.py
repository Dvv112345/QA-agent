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

        Validates that *md_bytes* is valid UTF-8 before writing.  Returns
        the absolute path to the saved file, or ``None`` when
        ``STORE_OFFLINE`` is disabled.
        """
        if not self._offline:
            return None

        # Fail loudly on invalid UTF-8 instead of silently corrupting data.
        try:
            text = md_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"README file is not valid UTF-8: {exc}") from exc

        sprint_dir = os.path.join(self._base, directory)
        os.makedirs(sprint_dir, exist_ok=True)

        readme_path = os.path.join(sprint_dir, "README.md")
        with open(readme_path, "w", encoding="utf-8") as fh:
            fh.write(text)

        logger.info("Stored README → %s", readme_path)
        return os.path.abspath(readme_path)

    def store_prd(self, content: bytes, directory: str, filename: str) -> str | None:
        """Persist an uploaded PRD file to disk if offline mode is active.

        Writes the original bytes verbatim (PDF/DOCX are binary) as
        ``PRD<original extension>``; a later upload overwrites it, matching
        the overwrite semantics of the PRD-derived requirements.  Returns
        the absolute path, or ``None`` when ``STORE_OFFLINE`` is disabled.
        """
        if not self._offline:
            return None

        _, ext = os.path.splitext(filename)
        sprint_dir = os.path.join(self._base, directory)
        os.makedirs(sprint_dir, exist_ok=True)

        prd_path = os.path.join(sprint_dir, f"PRD{ext.lower()}")
        with open(prd_path, "wb") as fh:
            fh.write(content)

        logger.info("Stored PRD → %s", prd_path)
        return os.path.abspath(prd_path)

    def store_screenshot(
        self, png: bytes, directory: str, session_id: int, position: int
    ) -> str | None:
        """Persist an exploratory finding's screenshot if offline mode is active.

        Returns the path, or ``None`` when ``STORE_OFFLINE`` is disabled — in
        which case findings simply carry no screenshot.  That is the normal
        outcome for that setting, not an error: callers must treat ``None`` as
        "no image available" rather than a failure.
        """
        if not self._offline:
            return None

        session_dir = os.path.join(self._base, directory, "exploratory", f"session_{session_id}")
        os.makedirs(session_dir, exist_ok=True)

        screenshot_path = os.path.join(session_dir, f"finding_{position}.png")
        with open(screenshot_path, "wb") as fh:
            fh.write(png)

        logger.info("Stored finding screenshot → %s", screenshot_path)
        return os.path.abspath(screenshot_path)
