"""GitHub API helpers for repo validation, metadata, and README retrieval."""

import base64
import logging
import re
import ssl
from typing import Any

import certifi
import httpx

from backend.config import FILE_TREE_MAX_CHARS, GITHUB_API_TIMEOUT

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

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
    async with httpx.AsyncClient(verify=_SSL_CONTEXT) as client:
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
    async with httpx.AsyncClient(verify=_SSL_CONTEXT) as client:
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
    async with httpx.AsyncClient(verify=_SSL_CONTEXT) as client:
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


# ── Repo file tree ───────────────────────────────────────────────────

# Directory names excluded from the file tree (matched against every
# path segment except the filename).
TREE_EXCLUDED_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    "out",
    "coverage",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
}

# Filename suffixes excluded from the file tree: binaries/media,
# minified assets, and source maps.
TREE_EXCLUDED_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".mp4",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".zip",
    ".gz",
    ".jar",
    ".exe",
    ".dll",
    ".so",
    ".pyc",
    ".min.js",
    ".min.css",
    ".map",
)

# Exact filenames excluded from the file tree (lockfiles and OS noise).
TREE_EXCLUDED_FILENAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Cargo.lock",
    ".DS_Store",
}

_TREE_TRUNCATION_MARKER = "\n… (truncated)"


def is_relevant_tree_path(path: str) -> bool:
    """Return whether a repo file path is worth including in LLM prompt context."""
    parts = path.split("/")
    if any(segment in TREE_EXCLUDED_DIRS for segment in parts[:-1]):
        return False
    filename = parts[-1]
    if filename in TREE_EXCLUDED_FILENAMES:
        return False
    return not filename.lower().endswith(TREE_EXCLUDED_SUFFIXES)


async def fetch_file_tree(
    owner: str,
    repo: str,
    default_branch: str,
    token: str | None = None,
) -> str | None:
    """Fetch the repo's file listing as newline-separated paths.

    Irrelevant files (binaries, lockfiles, dependency directories) are
    filtered out, and the result is capped at ``FILE_TREE_MAX_CHARS``.
    Returns ``None`` when the tree is empty or unavailable (404 — e.g. an
    empty repository), mirroring ``download_readme``'s no-README semantics.
    Raises the appropriate ``GitHubError`` subclass on other failures.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{default_branch}?recursive=1"
    async with httpx.AsyncClient(verify=_SSL_CONTEXT) as client:
        try:
            data = await _get(client, url, token)
        except RepoNotFoundError:
            return None

    paths = [
        entry["path"]
        for entry in data.get("tree", [])
        if entry.get("type") == "blob" and is_relevant_tree_path(entry.get("path", ""))
    ]
    if not paths:
        return None

    text = "\n".join(paths)
    truncated = bool(data.get("truncated"))
    if len(text) > FILE_TREE_MAX_CHARS:
        text = text[:FILE_TREE_MAX_CHARS]
        truncated = True
    if truncated:
        text += _TREE_TRUNCATION_MARKER
    return text
