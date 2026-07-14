"""Tests for backend/utils/github_utils.py — URL parsing, exceptions, API helpers."""

import base64

import pytest
from pytest_httpx import HTTPXMock

from backend.utils.github_utils import (
    AuthenticationRequiredError,
    GitHubUnavailableError,
    RateLimitedError,
    RepoNotFoundError,
    TokenInvalidError,
    check_readme_exists,
    download_readme,
    fetch_file_tree,
    fetch_repo_metadata,
    is_relevant_tree_path,
    parse_github_url,
)


class TestParseGithubUrl:
    """Tests for ``parse_github_url()``."""

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://github.com/owner/repo", ("owner", "repo")),
            ("https://github.com/owner/repo/", ("owner", "repo")),
            ("https://github.com/owner/repo.git", ("owner", "repo")),
            ("http://github.com/owner/repo", ("owner", "repo")),
            ("https://github.com/Owner/Repo-name", ("Owner", "Repo-name")),
        ],
    )
    def test_valid_urls(self, url, expected):
        assert parse_github_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "not-a-url",
            "https://gitlab.com/owner/repo",
            "https://github.com/owner",
            "https://github.com/owner/repo/issues",
            "https://github.com/owner/repo/tree/main",
        ],
    )
    def test_invalid_urls(self, url):
        with pytest.raises(ValueError):
            parse_github_url(url)


class TestFetchRepoMetadata:
    """Tests for ``fetch_repo_metadata()``."""

    @pytest.mark.asyncio
    async def test_returns_metadata(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo",
            json={
                "full_name": "owner/repo",
                "description": "A test repo",
                "private": False,
                "clone_url": "https://github.com/owner/repo.git",
                "default_branch": "main",
            },
        )
        result = await fetch_repo_metadata("owner", "repo")
        assert result["full_name"] == "owner/repo"
        assert result["description"] == "A test repo"
        assert result["private"] is False
        assert result["clone_url"] == "https://github.com/owner/repo.git"
        assert result["default_branch"] == "main"

    @pytest.mark.asyncio
    async def test_returns_fallback_full_name(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo",
            json={},
        )
        result = await fetch_repo_metadata("owner", "repo")
        assert result["full_name"] == "owner/repo"

    @pytest.mark.asyncio
    async def test_raises_on_404(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url="https://api.github.com/repos/owner/nope", status_code=404)
        with pytest.raises(RepoNotFoundError):
            await fetch_repo_metadata("owner", "nope")

    @pytest.mark.asyncio
    async def test_raises_on_401_no_token(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url="https://api.github.com/repos/owner/private", status_code=401)
        with pytest.raises(AuthenticationRequiredError):
            await fetch_repo_metadata("owner", "private")

    @pytest.mark.asyncio
    async def test_raises_on_401_with_token(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url="https://api.github.com/repos/owner/private", status_code=401)
        with pytest.raises(TokenInvalidError):
            await fetch_repo_metadata("owner", "private", token="bad-token")

    @pytest.mark.asyncio
    async def test_raises_on_403(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url="https://api.github.com/repos/owner/repo", status_code=403)
        with pytest.raises(RateLimitedError):
            await fetch_repo_metadata("owner", "repo")

    @pytest.mark.asyncio
    async def test_raises_on_500(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url="https://api.github.com/repos/owner/repo", status_code=500)
        with pytest.raises(GitHubUnavailableError):
            await fetch_repo_metadata("owner", "repo")


class TestCheckReadmeExists:
    """Tests for ``check_readme_exists()``."""

    @pytest.mark.asyncio
    async def test_returns_true_on_200(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/readme",
            status_code=200,
        )
        assert await check_readme_exists("owner", "repo") is True

    @pytest.mark.asyncio
    async def test_returns_false_on_404(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/readme",
            status_code=404,
        )
        assert await check_readme_exists("owner", "repo") is False

    @pytest.mark.asyncio
    async def test_raises_on_other_error(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/readme",
            status_code=500,
        )
        with pytest.raises(GitHubUnavailableError):
            await check_readme_exists("owner", "repo")


class TestDownloadReadme:
    """Tests for ``download_readme()``."""

    @pytest.mark.asyncio
    async def test_returns_decoded_content(self, httpx_mock: HTTPXMock):
        content = "# Hello World\nThis is a README."
        encoded = base64.b64encode(content.encode()).decode()
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/readme",
            json={"content": encoded},
        )
        result = await download_readme("owner", "repo")
        assert result == content

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/readme",
            status_code=404,
        )
        result = await download_readme("owner", "repo")
        assert result is None


class TestIsRelevantTreePath:
    """Tests for ``is_relevant_tree_path()``."""

    @pytest.mark.parametrize(
        "path",
        [
            "src/main.py",
            "README.md",
            "frontend/src/App.tsx",
            "docs/guide/setup.md",
            "distribution/notes.txt",  # 'dist' must match whole segments only
        ],
    )
    def test_relevant_paths(self, path):
        assert is_relevant_tree_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "node_modules/react/index.js",
            "frontend/node_modules/lodash/lodash.js",
            "dist/bundle.js",
            "__pycache__/main.cpython-312.pyc",
            ".venv/lib/site.py",
            "logo.png",
            "assets/video.mp4",
            "static/app.min.js",
            "static/app.js.map",
            "package-lock.json",
            "backend/poetry.lock",
            ".DS_Store",
        ],
    )
    def test_excluded_paths(self, path):
        assert is_relevant_tree_path(path) is False


class TestFetchFileTree:
    """Tests for ``fetch_file_tree()``."""

    _URL = "https://api.github.com/repos/owner/repo/git/trees/main?recursive=1"

    @pytest.mark.asyncio
    async def test_builds_filtered_path_list(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=self._URL,
            json={
                "tree": [
                    {"path": "README.md", "type": "blob"},
                    {"path": "src", "type": "tree"},
                    {"path": "src/main.py", "type": "blob"},
                    {"path": "node_modules/react/index.js", "type": "blob"},
                    {"path": "logo.png", "type": "blob"},
                    {"path": "package-lock.json", "type": "blob"},
                ],
                "truncated": False,
            },
        )
        result = await fetch_file_tree("owner", "repo", "main")
        assert result == "README.md\nsrc/main.py"

    @pytest.mark.asyncio
    async def test_marks_api_truncation(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=self._URL,
            json={
                "tree": [{"path": "a.py", "type": "blob"}],
                "truncated": True,
            },
        )
        result = await fetch_file_tree("owner", "repo", "main")
        assert result is not None
        assert result.endswith("(truncated)")

    @pytest.mark.asyncio
    async def test_caps_oversized_tree(self, httpx_mock: HTTPXMock, monkeypatch):
        monkeypatch.setattr("backend.utils.github_utils.FILE_TREE_MAX_CHARS", 10)
        httpx_mock.add_response(
            url=self._URL,
            json={
                "tree": [
                    {"path": "aaaaaaaa.py", "type": "blob"},
                    {"path": "bbbbbbbb.py", "type": "blob"},
                ],
                "truncated": False,
            },
        )
        result = await fetch_file_tree("owner", "repo", "main")
        assert result is not None
        assert result.endswith("(truncated)")
        assert len(result) <= 10 + len("\n… (truncated)")

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, status_code=404)
        assert await fetch_file_tree("owner", "repo", "main") is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_tree(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, json={"tree": [], "truncated": False})
        assert await fetch_file_tree("owner", "repo", "main") is None

    @pytest.mark.asyncio
    async def test_raises_on_500(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, status_code=500)
        with pytest.raises(GitHubUnavailableError):
            await fetch_file_tree("owner", "repo", "main")
