"""Tests for backend/utils/zip_utils.py — tree builder, zip extraction, security."""

import io
import zipfile
from pathlib import Path

import pytest

from backend.utils.zip_utils import (
    _cleanup_extraction,
    _simple_tree_text,
    extract_and_list_tree,
    extract_zip,
)


class TestSimpleTreeText:
    """Tests for ``_simple_tree_text``."""

    def test_empty_entries(self):
        assert _simple_tree_text([]) == ""

    def test_flat_list(self):
        result = _simple_tree_text(["b.txt", "a.txt"])
        lines = result.split("\n")
        assert len(lines) == 2
        # Sorted alphabetically
        assert "a.txt" in lines[0]
        assert "b.txt" in lines[1]

    def test_nested_structure(self):
        entries = ["src/main.py", "src/utils.py", "README.md"]
        result = _simple_tree_text(entries)
        # README.md at root, src/ folder with children
        assert "README.md" in result
        assert "src" in result
        assert "main.py" in result
        assert "utils.py" in result

    def test_nested_directories(self):
        entries = [
            "a/b/c/d/e/f/g/h/i/j/file.txt",
        ]
        result = _simple_tree_text(entries)
        # Should show the full nesting chain
        assert "file.txt" in result

    def test_max_depth_exceeded(self):
        # Create a path deeper than max_depth=5
        deep_path = "/".join(f"level{i}" for i in range(10))
        entries = [deep_path]
        with pytest.raises(ValueError, match="Maximum tree depth"):
            _simple_tree_text(entries, max_depth=5)


class TestExtractZip:
    """Tests for ``extract_zip``."""

    def _make_zip(self, files: dict) -> bytes:
        """Create an in-memory zip from a dict of {path: content}."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for path, content in files.items():
                if content is None:
                    # Simulate a directory entry
                    zf.writestr(zipfile.ZipInfo(path), b"")
                else:
                    zf.writestr(path, content)
        return buf.getvalue()

    def test_extracts_valid_archive(self, tmp_path):
        zip_bytes = self._make_zip({"hello.py": "print('hi')", "README.md": "# Project"})
        target = str(tmp_path / "extracted")
        extracted = extract_zip(zip_bytes, target, max_files=100, max_depth=10, chunk_size=8192)
        assert len(extracted) == 2
        assert "hello.py" in extracted
        assert "README.md" in extracted
        assert (tmp_path / "extracted" / "hello.py").read_text() == "print('hi')"

    def test_max_files_exceeded(self, tmp_path):
        zip_bytes = self._make_zip({f"{i}.txt": "x" for i in range(10)})
        with pytest.raises(ValueError, match="exceeds limit"):
            extract_zip(zip_bytes, str(tmp_path), max_files=5, max_depth=10, chunk_size=8192)

    def test_max_depth_exceeded(self, tmp_path):
        # Create a deeply nested file
        zip_bytes = self._make_zip({"a/b/c/d/e/f/g/h/i/j/file.txt": "deep"})
        with pytest.raises(ValueError, match="depth"):
            extract_zip(zip_bytes, str(tmp_path), max_files=100, max_depth=5, chunk_size=8192)

    def test_absolute_path_rejected(self, tmp_path):
        """On all platforms, a zip entry with a leading slash is blocked — either
        as an absolute path (Unix) or as path traversal (Windows where ``/foo`` is
        a relative path on the current drive)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("/etc/passwd", "malicious")
        with pytest.raises(ValueError):
            extract_zip(buf.getvalue(), str(tmp_path), max_files=100, max_depth=10, chunk_size=8192)

    def test_path_traversal_rejected(self, tmp_path):
        """Entries with ``..`` parts are caught by the explicit path traversal
        check (which runs before the dot-prefix filter)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("normal.txt", "safe")
            zf.writestr("../outside.txt", "escape")
        with pytest.raises(ValueError, match="path traversal"):
            extract_zip(buf.getvalue(), str(tmp_path), max_files=100, max_depth=10, chunk_size=8192)

    def test_ignores_dot_prefix_dirs(self, tmp_path):
        zip_bytes = self._make_zip({".hidden/config": "secret", "visible.txt": "ok"})
        extracted = extract_zip(
            zip_bytes, str(tmp_path), max_files=100, max_depth=10, chunk_size=8192
        )
        assert "visible.txt" in extracted
        assert not any(".hidden" in f for f in extracted)

    def test_ignores_pycache(self, tmp_path):
        zip_bytes = self._make_zip({"__pycache__/module.pyc": b"\x00" * 4, "module.py": "code"})
        extracted = extract_zip(
            zip_bytes, str(tmp_path), max_files=100, max_depth=10, chunk_size=8192
        )
        assert "module.py" in extracted
        assert not any("__pycache__" in f for f in extracted)

    def test_ignores_node_modules(self, tmp_path):
        zip_bytes = self._make_zip({"node_modules/pkg/index.js": "js", "src/main.ts": "ts"})
        extracted = extract_zip(
            zip_bytes, str(tmp_path), max_files=100, max_depth=10, chunk_size=8192
        )
        assert "src/main.ts" in extracted
        assert not any("node_modules" in f for f in extracted)

    def test_creates_subdirectories(self, tmp_path):
        zip_bytes = self._make_zip({"deep/nested/file.txt": "content"})
        target = str(tmp_path / "out")
        extract_zip(zip_bytes, target, max_files=100, max_depth=10, chunk_size=8192)
        assert (tmp_path / "out" / "deep" / "nested" / "file.txt").read_text() == "content"

    def test_directory_entries(self, tmp_path):
        """Directory entries in the zip are preserved in the extracted_files list."""
        zip_bytes = self._make_zip({"adir/": None, "adir/file.txt": "yes"})
        extracted = extract_zip(
            zip_bytes, str(tmp_path), max_files=100, max_depth=10, chunk_size=8192
        )
        assert "adir/" in extracted
        assert "adir/file.txt" in extracted


class TestExtractAndListTree:
    """Tests for ``extract_and_list_tree``."""

    def _make_zip(self, files: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for path, content in files.items():
                zf.writestr(path, content)
        return buf.getvalue()

    def test_returns_tree_list_and_text(self):
        zip_bytes = self._make_zip({"README.md": "# Hi", "src/app.py": "print(1)"})
        tree_list, tree_text, files = extract_and_list_tree(
            zip_bytes, stored_path=None, max_files=100, max_depth=10, chunk_size=8192
        )
        assert isinstance(tree_list, list)
        assert isinstance(tree_text, str)
        assert isinstance(files, list)
        assert "README.md" in tree_list
        assert "src/app.py" in tree_list
        assert "README.md" in tree_text
        assert "app.py" in tree_text

    def test_files_list_contains_only_file_paths(self):
        """The *files* return value excludes directory entries (no trailing ``/``)."""
        zip_bytes = self._make_zip(
            {
                "README.md": "# Hi",
                "src/app.py": "print(1)",
                "src/utils/helpers.py": "def f(): pass",
            }
        )
        _tree, _text, files = extract_and_list_tree(
            zip_bytes, stored_path=None, max_files=100, max_depth=10, chunk_size=8192
        )
        assert "README.md" in files
        assert "src/app.py" in files
        assert "src/utils/helpers.py" in files
        # Directory entries must not appear in files
        assert not any(f.endswith("/") for f in files)
        assert not any(f == "src" for f in files)
        assert not any(f == "src/utils" for f in files)

    def test_stored_path_variant(self, tmp_path):
        """When stored_path is provided, read tree from disk instead of extracting."""
        zip_bytes = self._make_zip({"a.txt": "a"})
        target = str(tmp_path / "stored")
        extract_zip(zip_bytes, target, max_files=100, max_depth=10, chunk_size=8192)

        tree_list, tree_text, files = extract_and_list_tree(
            zip_bytes, stored_path=target, max_files=100, max_depth=10, chunk_size=8192
        )
        assert "a.txt" in tree_list
        assert "a.txt" in tree_text
        assert "a.txt" in files

    def test_empty_archive(self):
        zip_bytes = self._make_zip({})
        tree_list, tree_text, files = extract_and_list_tree(
            zip_bytes, stored_path=None, max_files=100, max_depth=10, chunk_size=8192
        )
        assert tree_list == []
        assert tree_text == ""
        assert files == []


class TestCleanupExtraction:
    """Tests for ``_cleanup_extraction``."""

    def test_cleans_up_files(self, tmp_path):
        target = tmp_path / "cleanme"
        target.mkdir()
        (target / "a.txt").write_text("x")
        (target / "b.txt").write_text("y")

        _cleanup_extraction(target)
        assert not (target / "a.txt").exists()
        assert not (target / "b.txt").exists()

    def test_handles_nonexistent_path(self):
        # Should not raise
        _cleanup_extraction(Path("/nonexistent/path/for/cleanup/test"))
