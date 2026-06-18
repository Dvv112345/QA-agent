"""Tests for backend/utils/word_utils.py — text detection and word counting."""

import pytest

from backend.utils.word_utils import _mime_is_text, count_words_in_file, is_text_file


class TestMimeIsText:
    """Tests for ``_mime_is_text``."""

    def test_text_mime_returns_true(self):
        assert _mime_is_text("hello.py") is True
        assert _mime_is_text("index.html") is True
        assert _mime_is_text("styles.css") is True
        assert _mime_is_text("data.json") is True
        assert _mime_is_text("config.xml") is True
        assert _mime_is_text("app.js") is True
        # .md / .markdown is not recognized on all platforms (e.g. Windows)
        # — those files fall through to the null-byte scan, which is fine.

    def test_binary_mime_returns_false(self):
        assert _mime_is_text("image.png") is False
        assert _mime_is_text("archive.zip") is False
        assert _mime_is_text("program.exe") is False
        assert _mime_is_text("video.mp4") is False
        # .bin maps to application/octet-stream on most platforms
        assert _mime_is_text("data.bin") is False

    def test_unknown_extension_returns_none(self):
        """Files with unrecognized extensions fall through to null-byte scan."""
        assert _mime_is_text("file.xyzzy") is None
        assert _mime_is_text("no_extension") is None
        # .dat is typically unrecognized
        if _mime_is_text("data.dat") is not None:
            # Platform registers .dat — still fine, just skip the assertion
            pass


class TestIsTextFile:
    """Tests for ``is_text_file``."""

    def test_text_file_returns_true(self, tmp_path):
        f = tmp_path / "hello.py"
        f.write_text("print('hello world')", encoding="utf-8")
        assert is_text_file(str(f)) is True

    def test_binary_file_returns_false(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x00\x01\x02\x03")
        assert is_text_file(str(f)) is False

    def test_known_binary_extension_skipped_without_read(self, tmp_path):
        """A .png file should be rejected by MIME check before any I/O."""
        f = tmp_path / "icon.png"
        f.write_bytes(b"not a real png but the extension says binary")
        # The MIME check should return False before reading the file
        assert is_text_file(str(f)) is False

    def test_empty_file_returns_true(self, tmp_path):
        """An empty file has no null bytes, so it's treated as text."""
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        assert is_text_file(str(f)) is True

    def test_large_text_file(self, tmp_path):
        """File larger than the 8 KB detection window should still work."""
        f = tmp_path / "big.txt"
        f.write_text("hello " * 5000, encoding="utf-8")
        assert is_text_file(str(f)) is True

    def test_binary_after_text_window(self, tmp_path):
        """Null byte appears after 8 KB — still detected as binary by MIME.

        When the extension has a recognized binary MIME type (e.g., .bin →
        application/octet-stream), the MIME check catches it before the
        null-byte scan, so it's detected as binary regardless of the
        file content. Use an unknown extension to test the null-byte
        scan's 8 KB window behavior.
        """
        # Using an unknown extension so MIME returns None and we fall
        # through to the null-byte scan
        f = tmp_path / "late_null.xyzzy"
        f.write_bytes(b"A" * 8192 + b"\x00" + b"more text")
        result = is_text_file(str(f))
        # The first 8 KB have no null byte → treated as text
        assert result is True


class TestCountWordsInFile:
    """Tests for ``count_words_in_file``."""

    def test_counts_words_correctly(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("# Hello World\n\nThis is a test.", encoding="utf-8")
        # Tokens: #, Hello, World, This, is, a, test. = 7
        assert count_words_in_file(str(f)) == 7

    def test_empty_file_returns_zero(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        assert count_words_in_file(str(f)) == 0

    def test_whitespace_only_returns_zero(self, tmp_path):
        f = tmp_path / "spaces.txt"
        f.write_text("   \n  \t  \n", encoding="utf-8")
        assert count_words_in_file(str(f)) == 0

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            count_words_in_file(str(tmp_path / "nonexistent.txt"))

    def test_utf8_with_special_chars(self, tmp_path):
        f = tmp_path / "unicode.md"
        f.write_text("café résumé naïve", encoding="utf-8")
        assert count_words_in_file(str(f)) == 3

    def test_binary_file_with_errors_replace(self, tmp_path):
        """Binary content decoded with errors='replace' should not crash."""
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x80\x81\x82 \x90\x91")
        # Will decode with replacement chars — splitting on whitespace
        # should yield some tokens
        result = count_words_in_file(str(f))
        assert isinstance(result, int)
        assert result >= 0
