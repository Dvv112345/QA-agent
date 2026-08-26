"""Tests for backend/utils/github_utils.py — URL parsing, exceptions, API helpers."""

import base64
import json as json_lib

import httpx
import pytest
from pytest_httpx import HTTPXMock

from backend.utils.github_utils import (
    AuthenticationRequiredError,
    GitHubError,
    GitHubUnavailableError,
    RateLimitedError,
    RepoNotFoundError,
    TokenInvalidError,
    check_readme_exists,
    check_write_access,
    create_commit,
    create_pull_request,
    create_ref,
    create_tree,
    download_readme,
    fetch_file,
    fetch_file_tree,
    fetch_repo_metadata,
    get_branch_sha,
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


class TestFetchFile:
    """Tests for ``fetch_file()`` — the contents API used by the plan tool loop."""

    _URL = "https://api.github.com/repos/owner/repo/contents/src/app.py"

    @staticmethod
    def _file_json(text: str, **overrides) -> dict:
        payload = {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(text.encode("utf-8")).decode(),
            "size": len(text),
        }
        payload.update(overrides)
        return payload

    @pytest.mark.asyncio
    async def test_decodes_base64_content(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, json=self._file_json("print('hello')\n"))
        assert await fetch_file("owner", "repo", "src/app.py") == "print('hello')\n"

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, status_code=404)
        assert await fetch_file("owner", "repo", "src/app.py") is None

    @pytest.mark.asyncio
    async def test_ref_forwarded_as_query_param(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=f"{self._URL}?ref=dev", json=self._file_json("x = 1"))
        assert await fetch_file("owner", "repo", "src/app.py", ref="dev") == "x = 1"

    @pytest.mark.asyncio
    async def test_path_is_url_quoted(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/contents/docs/release%20notes.md",
            json=self._file_json("# Notes"),
        )
        assert await fetch_file("owner", "repo", "docs/release notes.md") == "# Notes"

    @pytest.mark.asyncio
    async def test_directory_listing_raises(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/contents/src",
            json=[{"type": "file", "name": "app.py", "path": "src/app.py"}],
        )
        with pytest.raises(GitHubError, match="not a readable text file"):
            await fetch_file("owner", "repo", "src")

    @pytest.mark.asyncio
    async def test_missing_content_raises(self, httpx_mock: HTTPXMock):
        # The contents API omits inline content for files over 1 MB.
        httpx_mock.add_response(
            url=self._URL,
            json={"type": "file", "encoding": "none", "content": "", "size": 2_000_000},
        )
        with pytest.raises(GitHubError, match="not a readable text file"):
            await fetch_file("owner", "repo", "src/app.py")

    @pytest.mark.asyncio
    async def test_undecodable_content_raises(self, httpx_mock: HTTPXMock):
        binary = base64.b64encode(b"\xff\xfe\x00\x01binary").decode()
        httpx_mock.add_response(
            url=self._URL,
            json={"type": "file", "encoding": "base64", "content": binary, "size": 10},
        )
        with pytest.raises(GitHubError, match="not a readable text file"):
            await fetch_file("owner", "repo", "src/app.py")

    @pytest.mark.asyncio
    async def test_401_without_token_raises_auth_required(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, status_code=401)
        with pytest.raises(AuthenticationRequiredError):
            await fetch_file("owner", "repo", "src/app.py")

    @pytest.mark.asyncio
    async def test_401_with_token_raises_token_invalid(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, status_code=401)
        with pytest.raises(TokenInvalidError):
            await fetch_file("owner", "repo", "src/app.py", token="ghp_x")

    @pytest.mark.asyncio
    async def test_403_raises_rate_limited(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, status_code=403)
        with pytest.raises(RateLimitedError):
            await fetch_file("owner", "repo", "src/app.py")

    @pytest.mark.asyncio
    async def test_500_raises_unavailable(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, status_code=500)
        with pytest.raises(GitHubUnavailableError):
            await fetch_file("owner", "repo", "src/app.py")


# ── Write helpers (CI/CD export) ─────────────────────────────────────


@pytest.fixture
async def write_client():
    """One client, as the export's write sequence uses it."""
    async with httpx.AsyncClient() as client:
        yield client


class TestCheckWriteAccess:
    """Tests for ``check_write_access()`` — the config-save gate."""

    _URL = "https://api.github.com/repos/owner/repo"

    @pytest.mark.asyncio
    async def test_true_when_push_is_granted(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, json={"permissions": {"push": True}})
        assert (await check_write_access("owner", "repo", "ghp_x")).can_push is True

    @pytest.mark.asyncio
    async def test_false_for_a_read_only_token(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, json={"permissions": {"pull": True, "push": False}})
        assert (await check_write_access("owner", "repo", "ghp_x")).can_push is False

    @pytest.mark.asyncio
    async def test_false_when_the_permissions_block_is_absent(self, httpx_mock: HTTPXMock):
        """GitHub omits it for unauthenticated reads — default to refusing."""
        httpx_mock.add_response(url=self._URL, json={"full_name": "owner/repo"})
        assert (await check_write_access("owner", "repo")).can_push is False

    @pytest.mark.asyncio
    async def test_404_raises_repo_not_found(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, status_code=404)
        with pytest.raises(RepoNotFoundError):
            await check_write_access("owner", "repo", "ghp_x")

    @pytest.mark.asyncio
    async def test_scopes_come_from_the_header(self, httpx_mock: HTTPXMock):
        """A classic token's grant is reported nowhere in the body."""
        httpx_mock.add_response(
            url=self._URL,
            json={"permissions": {"push": True}},
            headers={"X-OAuth-Scopes": "repo, workflow"},
        )

        access = await check_write_access("owner", "repo", "ghp_x")

        assert access.scopes == frozenset({"repo", "workflow"})
        assert access.lacks("workflow") is False

    @pytest.mark.asyncio
    async def test_a_pushable_token_can_still_lack_the_workflow_scope(self, httpx_mock: HTTPXMock):
        """The exact shape that failed: push granted, workflow files refused."""
        httpx_mock.add_response(
            url=self._URL,
            json={"permissions": {"admin": True, "push": True}},
            headers={"X-OAuth-Scopes": "repo"},
        )

        access = await check_write_access("owner", "repo", "ghp_x")

        assert access.can_push is True
        assert access.lacks("workflow") is True

    @pytest.mark.asyncio
    async def test_an_empty_scope_header_still_reports_a_grant(self, httpx_mock: HTTPXMock):
        """A classic token with no scopes says so — that is not "unknown"."""
        httpx_mock.add_response(
            url=self._URL,
            json={"permissions": {"push": True}},
            headers={"X-OAuth-Scopes": ""},
        )

        access = await check_write_access("owner", "repo", "ghp_x")

        assert access.scopes == frozenset()
        assert access.lacks("workflow") is True

    @pytest.mark.asyncio
    async def test_a_credential_reporting_no_scopes_is_never_declared_lacking(
        self, httpx_mock: HTTPXMock
    ):
        """Fine-grained PATs send no header — unknown must not read as missing."""
        httpx_mock.add_response(url=self._URL, json={"permissions": {"push": True}})

        access = await check_write_access("owner", "repo", "github_pat_x")

        assert access.scopes is None
        assert access.lacks("workflow") is False


class TestWriteSequence:
    """Tests for the tree -> commit -> ref -> PR helpers."""

    @pytest.mark.asyncio
    async def test_get_branch_sha_reads_the_ref_object(self, write_client, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/git/ref/heads/main",
            json={"object": {"sha": "base-sha"}},
        )
        sha = await get_branch_sha(write_client, "owner", "repo", "main", "ghp_x")
        assert sha == "base-sha"

    @pytest.mark.asyncio
    async def test_get_branch_sha_raises_when_the_ref_carries_no_commit(
        self, write_client, httpx_mock: HTTPXMock
    ):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/git/ref/heads/main", json={}
        )
        with pytest.raises(GitHubError, match="no commit to build on"):
            await get_branch_sha(write_client, "owner", "repo", "main", "ghp_x")

    @pytest.mark.asyncio
    async def test_create_tree_sends_inline_content_and_a_base_tree(
        self, write_client, httpx_mock: HTTPXMock
    ):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/git/trees",
            method="POST",
            json={"sha": "tree-sha"},
        )

        sha = await create_tree(
            write_client,
            "owner",
            "repo",
            "base-sha",
            {"qa-agent-tests/a_1/b_2.py": "print(1)\n"},
            "ghp_x",
        )

        assert sha == "tree-sha"
        payload = json_lib.loads(httpx_mock.get_requests()[-1].content)
        # base_tree is what makes the commit an addition rather than a tree
        # replacing everything else in the repository.
        assert payload["base_tree"] == "base-sha"
        entry = payload["tree"][0]
        assert entry == {
            "path": "qa-agent-tests/a_1/b_2.py",
            "mode": "100644",
            "type": "blob",
            "content": "print(1)\n",
        }
        assert "sha" not in entry  # inline content, not a blob reference

    @pytest.mark.asyncio
    async def test_create_tree_makes_no_blob_request(self, write_client, httpx_mock: HTTPXMock):
        """N files still cost one request — there is deliberately no create_blob."""
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/git/trees",
            method="POST",
            json={"sha": "tree-sha"},
        )

        await create_tree(
            write_client, "owner", "repo", "base-sha", {"a.py": "1", "b.py": "2", "c.py": "3"}, "t"
        )

        urls = [str(request.url) for request in httpx_mock.get_requests()]
        assert not any("/git/blobs" in url for url in urls)
        assert len(urls) == 1

    @pytest.mark.asyncio
    async def test_create_commit_sends_tree_and_parent(self, write_client, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/git/commits",
            method="POST",
            json={"sha": "commit-sha"},
        )

        sha = await create_commit(
            write_client, "owner", "repo", "Add QA tests", "tree-sha", "base-sha", "ghp_x"
        )

        assert sha == "commit-sha"
        payload = json_lib.loads(httpx_mock.get_requests()[-1].content)
        assert payload == {"message": "Add QA tests", "tree": "tree-sha", "parents": ["base-sha"]}

    @pytest.mark.asyncio
    async def test_create_ref_sends_a_full_refs_heads_path(
        self, write_client, httpx_mock: HTTPXMock
    ):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/git/refs",
            method="POST",
            json={"ref": "refs/heads/qa-agent/sprint-1"},
        )

        await create_ref(write_client, "owner", "repo", "qa-agent/sprint-1", "commit-sha", "ghp_x")

        payload = json_lib.loads(httpx_mock.get_requests()[-1].content)
        assert payload == {"ref": "refs/heads/qa-agent/sprint-1", "sha": "commit-sha"}

    @pytest.mark.asyncio
    async def test_create_ref_422_is_a_clean_error_carrying_githubs_message(
        self, write_client, httpx_mock: HTTPXMock
    ):
        """Two workers racing a restart can pick the same branch name."""
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/git/refs",
            method="POST",
            status_code=422,
            json={"message": "Reference already exists"},
        )

        with pytest.raises(GitHubError, match="Reference already exists"):
            await create_ref(
                write_client, "owner", "repo", "qa-agent/sprint-1", "commit-sha", "ghp_x"
            )

    @pytest.mark.asyncio
    async def test_create_pull_request_returns_number_and_url(
        self, write_client, httpx_mock: HTTPXMock
    ):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo/pulls",
            method="POST",
            json={"number": 7, "html_url": "https://github.com/owner/repo/pull/7"},
        )

        result = await create_pull_request(
            write_client, "owner", "repo", "QA tests", "body", "qa-agent/x", "main", "ghp_x"
        )

        assert result == {"number": 7, "html_url": "https://github.com/owner/repo/pull/7"}
        payload = json_lib.loads(httpx_mock.get_requests()[-1].content)
        assert payload["head"] == "qa-agent/x"
        assert payload["base"] == "main"


class TestWriteErrorMapping:
    """A failed write maps to the same exception hierarchy a read does."""

    _URL = "https://api.github.com/repos/owner/repo/git/trees"

    async def _create_tree(self, client):
        return await create_tree(client, "owner", "repo", "base", {"a.py": "1"}, "ghp_x")

    @pytest.mark.asyncio
    async def test_401_with_token_raises_token_invalid(self, write_client, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, method="POST", status_code=401)
        with pytest.raises(TokenInvalidError):
            await self._create_tree(write_client)

    @pytest.mark.asyncio
    async def test_404_raises_repo_not_found(self, write_client, httpx_mock: HTTPXMock):
        """A write has no `allow_404` analogue — absence is never a value here."""
        httpx_mock.add_response(url=self._URL, method="POST", status_code=404)
        with pytest.raises(RepoNotFoundError):
            await self._create_tree(write_client)

    @pytest.mark.asyncio
    async def test_404_on_a_workflow_file_names_the_workflow_scope(
        self, write_client, httpx_mock: HTTPXMock
    ):
        """GitHub's own answer is a bare 404 — the path is the only evidence.

        Pinned because the plain mapping sends the reader to check whether
        the repository exists, which the reads that precede this call have
        already answered.
        """
        httpx_mock.add_response(url=self._URL, method="POST", status_code=404)

        with pytest.raises(GitHubError) as exc:
            await create_tree(
                write_client,
                "owner",
                "repo",
                "base",
                {"qa-agent-tests/a.py": "1", ".github/workflows/qa.yml": "on: push"},
                "ghp_x",
            )

        assert "workflow" in str(exc.value)
        assert not isinstance(exc.value, RepoNotFoundError)

    @pytest.mark.asyncio
    async def test_403_raises_rate_limited(self, write_client, httpx_mock: HTTPXMock):
        """GitHub rate-limits writes harder than reads, and this turns into a retry."""
        httpx_mock.add_response(url=self._URL, method="POST", status_code=403)
        with pytest.raises(RateLimitedError):
            await self._create_tree(write_client)

    @pytest.mark.asyncio
    async def test_500_raises_unavailable(self, write_client, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=self._URL, method="POST", status_code=500)
        with pytest.raises(GitHubUnavailableError):
            await self._create_tree(write_client)

    @pytest.mark.asyncio
    async def test_timeout_raises_unavailable(self, write_client, httpx_mock: HTTPXMock):
        httpx_mock.add_exception(httpx.TimeoutException("too slow"))
        with pytest.raises(GitHubUnavailableError):
            await self._create_tree(write_client)


class TestRequestStaysAGetForReads:
    """Widening ``_request`` with a method must not change read behaviour."""

    @pytest.mark.asyncio
    async def test_get_still_issues_a_get(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/owner/repo",
            json={"full_name": "owner/repo", "default_branch": "main"},
        )

        await fetch_repo_metadata("owner", "repo")

        assert httpx_mock.get_requests()[-1].method == "GET"
