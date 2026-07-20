"""PRD document text extraction.

Dispatches on file extension: plain UTF-8 decode for text formats, pypdf
for ``.pdf``, python-docx for ``.docx``.  Every failure mode — unsupported
type, undecodable bytes, corrupt/encrypted documents, empty extraction —
surfaces as ``PrdExtractionError`` so callers can map it to a 422 without
knowing the parser libraries' exception zoos.
"""

import io
import os

import docx
from pypdf import PdfReader

# Allowed extensions for uploaded PRD documents.
PRD_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf", ".docx"}


class PrdExtractionError(Exception):
    """The PRD file could not be read as text (bad type, corrupt, or empty)."""


def extract_prd_text(filename: str, content: bytes) -> str:
    """Extract plain text from an uploaded PRD file.

    Raises ``PrdExtractionError`` on any unusable input; never lets a
    parser exception escape untyped.
    """
    _, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext not in PRD_EXTENSIONS:
        raise PrdExtractionError(
            f"Unsupported PRD file type {ext or 'none'} — use .md, .markdown, .txt, .pdf, or .docx."
        )

    if ext == ".pdf":
        text = _extract_pdf(content)
    elif ext == ".docx":
        text = _extract_docx(content)
    else:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PrdExtractionError("PRD file is not valid UTF-8.") from exc

    if not text.strip():
        raise PrdExtractionError(
            "No text could be extracted from the PRD — is it empty or a "
            "scanned image? Try a text-based export."
        )
    return text


def _extract_pdf(content: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise PrdExtractionError(f"Could not read the PDF file: {exc}") from exc


def _extract_docx(content: bytes) -> str:
    try:
        document = docx.Document(io.BytesIO(content))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    except Exception as exc:
        raise PrdExtractionError(f"Could not read the DOCX file: {exc}") from exc
