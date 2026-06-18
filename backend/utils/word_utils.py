"""Word-counting helpers for text files."""

from __future__ import annotations

import logging
import mimetypes

logger = logging.getLogger(__name__)

# Number of bytes to read from the start of a file to determine text vs binary.
_TEXT_DETECT_BYTES = 8192

# MIME types considered text for word-counting purposes.
_TEXT_MIME_PREFIXES = ("text/", "application/json", "application/xml", "application/javascript")


def _mime_is_text(path: str) -> bool | None:
    """Return ``True`` if *path*'s MIME type indicates text, ``False`` if it
    indicates binary, or ``None`` if the type could not be determined."""
    mime, _encoding = mimetypes.guess_type(path)
    if mime is None:
        return None
    return any(mime.startswith(prefix) for prefix in _TEXT_MIME_PREFIXES)


def is_text_file(path: str) -> bool:
    """Return ``True`` if *path* appears to be a text file.

    Uses two complementary checks:

    1. **MIME type** — if ``mimetypes`` recognizes the extension as a
       binary format the file is skipped without reading it.
    2. **Null-byte scan** — reads the first 8 KB of the file; if a null
       byte is present the file is treated as binary.

    Files whose MIME type cannot be determined fall through to the
    null-byte scan.
    """
    # ── MIME check (fast, no I/O) ─────────────────────────────────────
    mime_result = _mime_is_text(path)
    if mime_result is False:
        # Known binary extension — skip without reading
        logger.debug("%s has binary MIME type — skipping word count", path)
        return False

    # ── Null-byte scan ────────────────────────────────────────────────
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
