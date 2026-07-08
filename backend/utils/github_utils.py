"""GitHub API helpers for repo validation, metadata, and README retrieval."""

import base64
import logging
import re
from typing import Any

import httpx

from backend.config import GITHUB_API_TIMEOUT

logger = logging.getLogger(__name__)

# ── URL parsing ───────────────────────────────────────────────────────

_GITHUB_URL_RE = re.compile(r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


def parse_github_url(url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub URL.

    Raises ``ValueError`` if the URL doesn't match a known GitHub pattern.
    """
    m = _GITHUB_URL_RE.match(url.strip().rstrip("/"))
    if not m:
        raise ValueError(
            f"Invalid GitHub repository URL: {url!r}. "
            f"Expected format: https://github.com/owner/repo"
        )
    return m.group("owner"), m.group("repo")


# ── Exception hierarchy ──────────────────────────────────────────────


class GitHubError(Exception):
    """Base for all GitHub-related errors."""


class GitHubAccessError(GitHubError):
    """The repository is not accessible (404 or requires authentication)."""


class RepoNotFoundError(GitHubAccessError):
    """The repository does not exist or is private without a valid token."""


class AuthenticationRequiredError(GitHubError):
    """A token is required but was not provided."""


class TokenInvalidError(GitHubError):
    """The provided token is invalid or has been revoked."""


class RateLimitedError(GitHubError):
    """GitHub API rate limit has been exceeded."""


class GitHubUnavailableError(GitHubError):
    """GitHub API returned a 5xx or a network-level error."""


# ── HTTP client helper ───────────────────────────────────────────────


def _build_headers(token: str | None) -> dict[str, str]:
    """Return request headers for the GitHub API, optionally including auth."""
    headers: dict[str, str] = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "qa-agent",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _classify_error(status_code: int, has_token: bool) -> GitHubError:
    """Map an HTTP status code to the appropriate ``GitHubError`` subclass."""
    if status_code == 401:
        if has_token:
            return TokenInvalidError("GitHub token is invalid or has been revoked.")
        return AuthenticationRequiredError(
            "Authentication required — this repository may be private."
        )
    if status_code == 403:
        return RateLimitedError("GitHub API rate limit exceeded. Try again later.")
    if status_code == 404:
        return RepoNotFoundError("Repository not found or not accessible.")
    if 500 <= status_code < 600:
        return GitHubUnavailableError(f"GitHub returned {status_code} — service may be degraded.")
    return GitHubError(f"GitHub API returned unexpected status {status_code}.")


async def _get(
    client: httpx.AsyncClient,
    url: str,
    token: str | None,
) -> dict[str, Any]:
    """Perform an authenticated GET request and return parsed JSON.

    Raises the appropriate ``GitHubError`` subclass on failure.
    """
    headers = _build_headers(token)
    try:
        response = await client.get(url, headers=headers, timeout=GITHUB_API_TIMEOUT)
    except httpx.TimeoutException:
        raise GitHubUnavailableError(
            f"GitHub API request timed out after {GITHUB_API_TIMEOUT}s: {url}"
        ) from None
    except httpx.RequestError as exc:
        raise GitHubUnavailableError(f"Could not reach GitHub API: {exc}") from exc

    if not response.is_success:
        raise _classify_error(response.status_code, bool(token))

    return response.json()


# ── Public API helpers ───────────────────────────────────────────────


async def fetch_repo_metadata(owner: str, repo: str, token: str | None = None) -> dict[str, Any]:
    """Fetch repository metadata from the GitHub API.

    Returns a dict with keys: ``full_name``, ``description``, ``private``,
    ``clone_url``, ``default_branch``.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}"
    async with httpx.AsyncClient() as client:
        data = await _get(client, url, token)

    return {
        "full_name": data.get("full_name", f"{owner}/{repo}"),
        "description": data.get("description"),
        "private": data.get("private", False),
        "clone_url": data.get("clone_url", ""),
        "default_branch": data.get("default_branch", "main"),
    }


async def check_readme_exists(owner: str, repo: str, token: str | None = None) -> bool:
    """Check whether the GitHub repository has a README file.

    Returns ``True`` if the README API endpoint returns 200, ``False``
    on 404, and raises on other errors.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    headers = _build_headers(token)
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, timeout=GITHUB_API_TIMEOUT)
        except httpx.TimeoutException:
            raise GitHubUnavailableError(
                f"GitHub README check timed out after {GITHUB_API_TIMEOUT}s"
            ) from None
        except httpx.RequestError as exc:
            raise GitHubUnavailableError(f"Could not reach GitHub API: {exc}") from exc

    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False

    raise _classify_error(response.status_code, bool(token))


async def download_readme(owner: str, repo: str, token: str | None = None) -> str | None:
    """Download and decode the README content from a GitHub repository.

    Returns the raw markdown text, or ``None`` if the repository has no
    README (404).  Raises the appropriate ``GitHubError`` subclass on other
    failures.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    headers = _build_headers(token)
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, timeout=GITHUB_API_TIMEOUT)
        except httpx.TimeoutException:
            raise GitHubUnavailableError(
                f"GitHub README download timed out after {GITHUB_API_TIMEOUT}s"
            ) from None
        except httpx.RequestError as exc:
            raise GitHubUnavailableError(f"Could not reach GitHub API: {exc}") from exc

    if response.status_code == 200:
        content_b64 = response.json().get("content", "")
        return base64.b64decode(content_b64).decode("utf-8")
    if response.status_code == 404:
        return None

    raise _classify_error(response.status_code, bool(token))
