"""Tests for PRD text extraction (backend/utils/prd_utils.py)."""

from pathlib import Path

import pytest

from backend.utils.prd_utils import PrdExtractionError, extract_prd_text

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ── Happy paths per format ────────────────────────────────────────────


def test_markdown_decodes_utf8():
    assert extract_prd_text("prd.md", "# Title\n\nBody ✓".encode()) == "# Title\n\nBody ✓"


@pytest.mark.parametrize("filename", ["prd.txt", "prd.markdown", "PRD.MD"])
def test_text_extensions_case_insensitive(filename):
    assert extract_prd_text(filename, b"Some requirement text") == "Some requirement text"


def test_pdf_fixture_extracts_text():
    content = (_FIXTURES / "sample_prd.pdf").read_bytes()
    assert "upload a PRD document" in extract_prd_text("sample_prd.pdf", content)


def test_docx_fixture_extracts_text():
    content = (_FIXTURES / "sample_prd.docx").read_bytes()
    text = extract_prd_text("sample_prd.docx", content)
    assert "splits the PRD into requirements" in text


# ── Failure modes ─────────────────────────────────────────────────────


@pytest.mark.parametrize("filename", ["prd.doc", "prd.rtf", "prd", "prd.pdf.exe"])
def test_unsupported_extension(filename):
    with pytest.raises(PrdExtractionError, match="Unsupported PRD file type"):
        extract_prd_text(filename, b"whatever")


def test_text_file_not_utf8():
    with pytest.raises(PrdExtractionError, match="not valid UTF-8"):
        extract_prd_text("prd.md", b"\xff\xfe\x00bad")


def test_corrupt_pdf():
    with pytest.raises(PrdExtractionError, match="Could not read the PDF"):
        extract_prd_text("prd.pdf", b"%PDF-1.4 garbage that is not a pdf")


def test_corrupt_docx():
    with pytest.raises(PrdExtractionError, match="Could not read the DOCX"):
        extract_prd_text("prd.docx", b"PK\x03\x04 garbage that is not a docx")


@pytest.mark.parametrize("content", [b"", b"   \n\t  "])
def test_empty_or_whitespace_text(content):
    with pytest.raises(PrdExtractionError, match="No text could be extracted"):
        extract_prd_text("prd.md", content)
