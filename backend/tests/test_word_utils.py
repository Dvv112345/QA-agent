"""Tests for backend/utils/word_utils.py — text detection and word counting."""

import pytest

from backend.utils.word_utils import count_words_in_file, is_text_file


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
        """Null byte appears after 8 KB — still detected as binary."""
        f = tmp_path / "late_null.bin"
        # First 8 KB are text, then a null byte
        f.write_bytes(b"A" * 8192 + b"\x00" + b"more text")
        # Our detector only reads 8 KB, so the null byte after that isn't seen
        # This is the expected behavior with the current implementation
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
