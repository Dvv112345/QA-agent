"""Tests for backend/services/repo_reader.py — the shared read_file executor.

Four LLM stages hand the model this tool (script generation, script
diagnosis, CI/CD authoring, nonfunctional run planning), so its guarantees
are asserted once here rather than four times over.  The path guard is the
load-bearing one: it is the only thing standing between a model-supplied
string and a repository fetch.
"""

import pytest

from backend.services import repo_reader
from backend.utils import github_utils

FILE_TREE = "src/app.py\nREADME.md\ndocs/guide.md"


def _reader(monkeypatch, content="FILE CONTENT", error=None):
    """Build an executor over a stubbed fetch_file, recording what it asked for."""
    asked: list[tuple] = []

    async def _fetch(owner, repo, path, token=None, ref=None):
        asked.append((owner, repo, path, token))
        if error is not None:
            raise error
        return content

    monkeypatch.setattr(github_utils, "fetch_file", _fetch)
    return repo_reader.build_read_file(FILE_TREE, "owner", "repo", "tok"), asked


class TestPathGuard:
    def test_a_path_in_the_tree_is_read(self, monkeypatch):
        read_file, asked = _reader(monkeypatch)

        assert read_file("src/app.py") == "FILE CONTENT"
        assert asked == [("owner", "repo", "src/app.py", "tok")]

    def test_a_path_outside_the_tree_is_refused_without_fetching(self, monkeypatch):
        """The guard must refuse *before* the request, not judge the response."""
        read_file, asked = _reader(monkeypatch)

        result = read_file("../../etc/passwd")

        assert "is not in the repository file tree" in result
        assert asked == []

    @pytest.mark.parametrize("given", ["/src/app.py", "  src/app.py  ", "src/app.py"])
    def test_leading_slash_and_whitespace_are_normalized(self, monkeypatch, given):
        """A model writes paths inconsistently; these are the same file."""
        read_file, asked = _reader(monkeypatch)

        assert read_file(given) == "FILE CONTENT"
        assert asked[0][2] == "src/app.py"

    def test_an_empty_path_is_refused(self, monkeypatch):
        read_file, asked = _reader(monkeypatch)

        assert "is not in the repository file tree" in read_file("")
        assert asked == []


class TestNeverRaises:
    """Errors go back as strings the model can react to. A tool that raises
    ends the loop; one that explains itself lets the model try another path."""

    def test_a_github_error_becomes_an_error_string(self, monkeypatch):
        read_file, _ = _reader(monkeypatch, error=github_utils.GitHubError("rate limited"))

        result = read_file("src/app.py")

        assert result.startswith("ERROR: could not read 'src/app.py'")
        assert "rate limited" in result

    def test_a_missing_file_becomes_an_error_string(self, monkeypatch):
        read_file, _ = _reader(monkeypatch, content=None)

        assert "file not found" in read_file("src/app.py")


class TestTruncation:
    def test_oversized_content_is_truncated_with_a_marker(self, monkeypatch):
        monkeypatch.setattr(repo_reader, "TEST_EXECUTION_FILE_MAX_CHARS", 10)
        read_file, _ = _reader(monkeypatch, content="x" * 50)

        result = read_file("src/app.py")

        assert result == "x" * 10 + repo_reader._FILE_TRUNCATION_MARKER

    def test_content_at_the_cap_is_untouched(self, monkeypatch):
        monkeypatch.setattr(repo_reader, "TEST_EXECUTION_FILE_MAX_CHARS", 10)
        read_file, _ = _reader(monkeypatch, content="x" * 10)

        assert read_file("src/app.py") == "x" * 10
