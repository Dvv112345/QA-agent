"""Word-counting helpers for text files."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Number of bytes to read from the start of a file to determine text vs binary.
_TEXT_DETECT_BYTES = 8192


def is_text_file(path: str) -> bool:
    """Return ``True`` if *path* appears to be a text file.

    Reads the first 8 KB of the file; if a null byte is present the file
    is treated as binary.
    """
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(_TEXT_DETECT_BYTES)
    except OSError as exc:
        logger.warning("Cannot read %s for text detection: %s", path, exc)
        return False

    return b"\x00" not in chunk


def count_words_in_file(path: str) -> int:
    """Count whitespace-delimited tokens in a UTF-8 text file.

    Raises:
        FileNotFoundError: If *path* does not exist.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        raise
    except OSError as exc:
        logger.warning("Cannot read %s for word counting: %s", path, exc)
        return 0

    if not text.strip():
        return 0

    return len(text.split())
