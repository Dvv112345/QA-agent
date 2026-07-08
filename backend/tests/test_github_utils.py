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
    fetch_repo_metadata,
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
