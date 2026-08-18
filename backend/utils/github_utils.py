"""GitHub API helpers for repo validation, metadata, and README retrieval."""

import base64
import logging
import re
import urllib.parse
from typing import Any

import httpx

from backend.config import FILE_TREE_MAX_CHARS, GITHUB_API_TIMEOUT
from backend.utils.http_utils import SSL_CONTEXT

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


async def _request(
    client: httpx.AsyncClient,
    url: str,
    token: str | None,
    *,
    method: str = "GET",
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    """One authenticated request, with every transport failure mapped to ours.

    Returns the response whatever its status — the callers differ on which
    codes are fatal, and on whether they want the body at all.  Only
    failures that produced *no* response raise from here.

    ``method``/``json`` exist so the write helpers share this transport and
    its error mapping.  Everything above stays split exactly as it was:
    this layer never parses a body, and ``_get`` remains the only place
    that does (see the "share the transport, not the parsing" rule — a
    caller that discards the body must not inherit a parse that can fail).
    """
    headers = _build_headers(token)
    try:
        return await client.request(
            method, url, headers=headers, json=json, timeout=GITHUB_API_TIMEOUT
        )
    except httpx.TimeoutException:
        raise GitHubUnavailableError(
            f"GitHub API request timed out after {GITHUB_API_TIMEOUT}s: {url}"
        ) from None
    except httpx.RequestError as exc:
        raise GitHubUnavailableError(f"Could not reach GitHub API: {exc}") from exc


async def _get(
    client: httpx.AsyncClient,
    url: str,
    token: str | None,
    *,
    allow_404: bool = False,
) -> dict[str, Any] | None:
    """Perform an authenticated GET request and return parsed JSON.

    Raises the appropriate ``GitHubError`` subclass on failure.

    ``allow_404=True`` answers ``None`` for a missing resource instead of
    raising, for the callers where absence is a value rather than an error
    — a repository with no README is an ordinary repository.  Everything
    else still raises, so "not there" and "cannot tell" stay distinct.
    """
    response = await _request(client, url, token)

    if allow_404 and response.status_code == 404:
        return None
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
    async with httpx.AsyncClient(verify=SSL_CONTEXT) as client:
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
    async with httpx.AsyncClient(verify=SSL_CONTEXT) as client:
        # `_request`, not `_get`: existence is the whole question, and
        # parsing a body this caller discards would let a 200 with an
        # unexpected payload read as "no README".
        response = await _request(client, url, token)

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
    async with httpx.AsyncClient(verify=SSL_CONTEXT) as client:
        data = await _get(client, url, token, allow_404=True)

    if data is None:
        return None
    return base64.b64decode(data.get("content", "")).decode("utf-8")


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
    async with httpx.AsyncClient(verify=SSL_CONTEXT) as client:
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


# ── Repo file contents ───────────────────────────────────────────────


async def fetch_file(
    owner: str,
    repo: str,
    path: str,
    token: str | None = None,
    ref: str | None = None,
) -> str | None:
    """Fetch a repository file's text content via the GitHub contents API.

    Returns the decoded UTF-8 text, or ``None`` when the path does not
    exist (404).  A directory response or non-decodable content (binary,
    or files too large for the contents API to inline) raises
    ``GitHubError``; other HTTP failures raise the appropriate
    ``GitHubError`` subclass.
    """
    # Repo paths can contain spaces/# — quote each segment, keep separators.
    quoted_path = urllib.parse.quote(path, safe="/")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{quoted_path}"
    if ref:
        url += f"?ref={urllib.parse.quote(ref)}"

    async with httpx.AsyncClient(verify=SSL_CONTEXT) as client:
        data = await _get(client, url, token, allow_404=True)

    if data is None:
        return None
    # A directory returns a JSON list; files >1 MB come back without inline
    # content — neither is readable text for our purposes.
    if not isinstance(data, dict) or data.get("type") != "file" or not data.get("content"):
        raise GitHubError(f"{path!r} is not a readable text file.")
    try:
        return base64.b64decode(data["content"]).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise GitHubError(f"{path!r} is not a readable text file: {exc}") from exc


# ── Write helpers (CI/CD export) ─────────────────────────────────────
#
# Every helper below takes an open ``client`` as its first argument rather
# than opening one, because the whole branch-and-PR sequence is five
# sequential requests against the same host and there is exactly one
# ``async with`` above them.  That matches the split this module already
# had: public *read* helpers open a client, the layer beneath receives one.
#
# ``SSL_CONTEXT`` is therefore mandatory at that single construction site —
# on Windows a stale ``SSL_CERT_FILE`` breaks client creation outright.


def _write_error(response: httpx.Response, has_token: bool) -> GitHubError:
    """Map a failed write, keeping GitHub's own explanation when it has one.

    ``_classify_error`` answers a bare ``GitHubError`` for the statuses it
    does not recognise, and the one that matters most here — 422 from
    ``create_ref`` when a branch name already exists — is exactly such a
    status.  Without the API's message the retry is undiagnosable.
    """
    error = _classify_error(response.status_code, has_token)
    if type(error) is not GitHubError:
        return error
    try:
        message = response.json().get("message")
    except (ValueError, AttributeError):
        message = None
    if not message:
        return error
    return GitHubError(f"GitHub API returned {response.status_code}: {message}")


async def _write(
    client: httpx.AsyncClient,
    url: str,
    token: str | None,
    *,
    method: str = "POST",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One authenticated write, returning parsed JSON.

    Deliberately has no ``allow_404`` analogue: absence is a value on a read
    (a repository legitimately has no README), but a 404 on a write always
    means the target is wrong or the token cannot see it.
    """
    response = await _request(client, url, token, method=method, json=payload)
    if not response.is_success:
        raise _write_error(response, bool(token))
    return response.json()


async def check_push_permission(owner: str, repo: str, token: str | None = None) -> bool:
    """Whether ``token`` can push to ``owner/repo``.

    A stronger claim than the issue tracker's "can I file here", and checked
    when the credential is *saved* rather than after an export has already
    spent an LLM call on it.  A missing ``permissions`` block reads as
    ``False``: GitHub omits it for unauthenticated reads, and defaulting a
    permission question to "yes" is the wrong direction to be wrong in.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}"
    async with httpx.AsyncClient(verify=SSL_CONTEXT) as client:
        data = await _get(client, url, token)

    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return False
    return bool(permissions.get("push"))


async def get_branch_sha(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    branch: str,
    token: str | None = None,
) -> str:
    """The commit SHA a branch currently points at — the base of our commit."""
    url = f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}"
    data = await _get(client, url, token)
    sha = (data.get("object") or {}).get("sha")
    if not sha:
        raise GitHubError(f"Branch {branch!r} has no commit to build on.")
    return sha


async def create_tree(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    base_tree: str,
    files: dict[str, str],
    token: str | None = None,
) -> str:
    """Create a git tree extending ``base_tree`` with ``files``, return its SHA.

    File content rides **inline** on each entry rather than being uploaded
    as a blob first: GitHub writes the blob itself, so the whole export is
    four write requests regardless of how many test cases it ships, instead
    of one per file plus four.  The only bound is total request size, which
    text scripts do not approach.

    ``base_tree`` is what makes the commit an *addition* to the default
    branch rather than a tree that replaces everything else in the
    repository with these files alone.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees"
    payload = {
        "base_tree": base_tree,
        "tree": [
            {"path": path, "mode": "100644", "type": "blob", "content": content}
            for path, content in files.items()
        ],
    }
    data = await _write(client, url, token, payload=payload)
    return data["sha"]


async def create_commit(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    message: str,
    tree_sha: str,
    parent_sha: str,
    token: str | None = None,
) -> str:
    """Create a commit over ``tree_sha`` with one parent, return its SHA."""
    url = f"https://api.github.com/repos/{owner}/{repo}/git/commits"
    payload = {"message": message, "tree": tree_sha, "parents": [parent_sha]}
    data = await _write(client, url, token, payload=payload)
    return data["sha"]


async def create_ref(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    branch: str,
    sha: str,
    token: str | None = None,
) -> None:
    """Point a **new** branch at ``sha``.

    Always a create, never a force-update: the export's whole relationship
    with the target repository is append-only, and a fresh branch per
    attempt is what makes a retry idempotent.  A 422 here means the name is
    taken, which is a clean retryable failure rather than something to
    resolve by overwriting someone's branch.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/git/refs"
    await _write(client, url, token, payload={"ref": f"refs/heads/{branch}", "sha": sha})


async def create_pull_request(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
    token: str | None = None,
) -> dict[str, Any]:
    """Open a pull request, returning ``{"number", "html_url"}``.

    The PR *is* the deliverable: nothing here ever merges it, and the human
    review it invites is the gate that catches a generated workflow being
    wrong in a way no check of ours could.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    payload = {"title": title, "body": body, "head": head, "base": base}
    data = await _write(client, url, token, payload=payload)
    return {"number": data.get("number"), "html_url": data.get("html_url", "")}
