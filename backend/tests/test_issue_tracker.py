"""Tests for backend/services/issue_tracker.py — both providers, no network.

Every request is intercepted by ``pytest-httpx``, which covers the
synchronous client this module uses as well as the async ones elsewhere.
No test may hold a real credential: the tokens here are dummies.
"""

import httpx
import pytest

from backend.services.issue_tracker import (
    FindingContext,
    FindingReport,
    TrackerConfig,
    TrackerError,
    TrackerUnavailableError,
    attach_screenshot,
    create_issue,
    issue_is_open,
    verify,
)
from backend.utils.environment_utils import redact

_JIRA_SITE = "https://acme.atlassian.net"


def _jira_config(**overrides) -> TrackerConfig:
    defaults = {
        "provider": "jira",
        "target": "QA",
        "api_token": "dummy-token",
        "base_url": _JIRA_SITE,
        "account_email": "qa@acme.test",
        "issue_type": "Bug",
    }
    defaults.update(overrides)
    return TrackerConfig(**defaults)


def _github_config(**overrides) -> TrackerConfig:
    defaults = {"provider": "github", "target": "acme/shop", "api_token": "dummy-token"}
    defaults.update(overrides)
    return TrackerConfig(**defaults)


def _report(**overrides) -> FindingReport:
    defaults = {
        "finding_type": "bug",
        "severity": "high",
        "title": "Checkout total omits tax",
        "steps_to_reproduce": "Open /cart\nAdd an item\nProceed to checkout",
        "expected": "Total includes tax",
        "actual": "Total excludes tax",
        "environment": "Windows-10 · Python 3.12.4",
    }
    defaults.update(overrides)
    return FindingReport(**defaults)


def _context(**overrides) -> FindingContext:
    defaults = {
        "sprint_name": "Sprint 3",
        "requirement_name": "Checkout",
        "run_label": "Scripted run 14",
        "source_label": "Tax is applied to the order total",
        "source_kind": "scripted",
        "secrets": {},
    }
    defaults.update(overrides)
    return FindingContext(**defaults)


_JIRA_ISSUETYPES_URL = f"{_JIRA_SITE}/rest/api/3/issue/createmeta/QA/issuetypes?maxResults=200"


def _jira_verify_responses(httpx_mock, *, issue_types=("Bug", "Task")):
    httpx_mock.add_response(url=f"{_JIRA_SITE}/rest/api/3/myself", json={"accountId": "abc"})
    httpx_mock.add_response(
        url=f"{_JIRA_SITE}/rest/api/3/project/QA", json={"name": "Quality Assurance"}
    )
    # The per-project shape. The older `createmeta?projectKeys=&expand=`
    # form is deprecated and current Cloud sites answer it without the
    # `projects` key it was read through — which surfaced as "issue type
    # does not exist" rather than as an API error.
    httpx_mock.add_response(
        url=_JIRA_ISSUETYPES_URL,
        json={
            "maxResults": 200,
            "startAt": 0,
            "total": len(issue_types),
            "issueTypes": [{"id": str(i), "name": name} for i, name in enumerate(issue_types)],
        },
    )


class TestSecretsStayOutOfRepr:
    """A credential reaching a log through a generated __repr__ is the
    kind of leak nothing in the module's own discipline would catch."""

    def test_the_token_is_not_in_the_config_repr(self):
        assert "dummy-token" not in repr(_jira_config())

    def test_env_values_are_not_in_the_context_repr(self):
        context = _context(secrets={"QA_PASSWORD": "s3cr3t-passw0rd"})
        assert "s3cr3t-passw0rd" not in repr(context)


class TestJiraVerify:
    def test_succeeds_and_returns_a_display_label(self, httpx_mock):
        _jira_verify_responses(httpx_mock)
        assert verify(_jira_config()) == "Quality Assurance (QA)"

    def test_reads_issue_types_from_the_per_project_endpoint(self, httpx_mock):
        """Pinned against the URL, because the deprecated form fails in the
        one way a test of the *result* cannot see: current Cloud sites
        answer it 200 with no `projects` key, which reads as "this project
        has no issue types" and refuses a valid config."""
        _jira_verify_responses(httpx_mock)

        verify(_jira_config())

        requested = [str(request.url) for request in httpx_mock.get_requests()]
        assert _JIRA_ISSUETYPES_URL in requested
        assert not any("projectKeys=" in url for url in requested)

    def test_bad_credentials_raise(self, httpx_mock):
        httpx_mock.add_response(url=f"{_JIRA_SITE}/rest/api/3/myself", status_code=401)
        with pytest.raises(TrackerError) as exc:
            verify(_jira_config())
        assert "credentials" in str(exc.value)

    def test_unknown_project_raises(self, httpx_mock):
        httpx_mock.add_response(url=f"{_JIRA_SITE}/rest/api/3/myself", json={"accountId": "abc"})
        httpx_mock.add_response(url=f"{_JIRA_SITE}/rest/api/3/project/QA", status_code=404)
        with pytest.raises(TrackerError) as exc:
            verify(_jira_config())
        assert "'QA'" in str(exc.value)

    def test_unknown_issue_type_raises_and_lists_the_real_ones(self, httpx_mock):
        """Validated at connect time because an unknown type 400s every
        create, long after the user has left the settings screen."""
        _jira_verify_responses(httpx_mock, issue_types=("Task", "Story"))
        with pytest.raises(TrackerError) as exc:
            verify(_jira_config(issue_type="Defect"))
        assert "Task, Story" in str(exc.value) or "Story, Task" in str(exc.value)

    def test_missing_issue_type_raises(self, httpx_mock):
        httpx_mock.add_response(url=f"{_JIRA_SITE}/rest/api/3/myself", json={"accountId": "abc"})
        httpx_mock.add_response(url=f"{_JIRA_SITE}/rest/api/3/project/QA", json={"name": "QA"})
        with pytest.raises(TrackerError):
            verify(_jira_config(issue_type=None))

    def test_server_error_is_unavailable_not_a_refusal(self, httpx_mock):
        """The route answers 502 for this and 422 for a refusal, so the
        split has to survive down here."""
        httpx_mock.add_response(url=f"{_JIRA_SITE}/rest/api/3/myself", status_code=503)
        with pytest.raises(TrackerUnavailableError):
            verify(_jira_config())

    def test_missing_base_url_raises(self):
        with pytest.raises(TrackerError):
            verify(_jira_config(base_url=None))


class TestGitHubVerify:
    def test_succeeds_and_returns_full_name(self, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/acme/shop",
            json={"full_name": "acme/shop", "has_issues": True},
        )
        assert verify(_github_config()) == "acme/shop"

    def test_succeeds_without_push_permission(self, httpx_mock):
        """A fine-grained token can hold Issues:write without push.
        Requiring push would reject exactly the careful tokens."""
        httpx_mock.add_response(
            url="https://api.github.com/repos/acme/shop",
            json={
                "full_name": "acme/shop",
                "has_issues": True,
                "permissions": {"push": False, "pull": True},
            },
        )
        assert verify(_github_config()) == "acme/shop"

    def test_issues_disabled_raises(self, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/acme/shop",
            json={"full_name": "acme/shop", "has_issues": False},
        )
        with pytest.raises(TrackerError) as exc:
            verify(_github_config())
        assert "Issues are disabled" in str(exc.value)

    def test_unknown_repo_raises(self, httpx_mock):
        httpx_mock.add_response(url="https://api.github.com/repos/acme/shop", status_code=404)
        with pytest.raises(TrackerError):
            verify(_github_config())

    def test_malformed_target_raises(self):
        with pytest.raises(TrackerError):
            verify(_github_config(target="shop"))


class TestUnknownProvider:
    def test_verify_raises(self):
        with pytest.raises(TrackerError):
            verify(_github_config(provider="gitlab"))


class TestJiraCreateIssue:
    def test_description_is_an_adf_document_not_a_string(self, httpx_mock):
        """Jira v3 rejects a plain-string description with a 400, so the
        payload shape is the contract — not a formatting preference."""
        httpx_mock.add_response(
            url=f"{_JIRA_SITE}/rest/api/3/issue", json={"key": "QA-142"}, status_code=201
        )

        ref = create_issue(_jira_config(), _report(), _context())

        request = httpx_mock.get_requests()[-1]
        fields = httpx.Response(200, content=request.content).json()["fields"]
        description = fields["description"]
        assert isinstance(description, dict)
        assert description["type"] == "doc"
        assert description["version"] == 1
        node_types = {node["type"] for node in description["content"]}
        assert node_types <= {"paragraph", "heading", "bulletList"}
        assert ref.key == "QA-142"
        assert ref.url == f"{_JIRA_SITE}/browse/QA-142"

    def test_payload_carries_only_the_portable_fields(self, httpx_mock):
        """Instance-specific required fields 400 the create, so the payload
        stays at the five every Jira project has. Severity rides as a
        label rather than as `priority`, which is instance-configured."""
        httpx_mock.add_response(url=f"{_JIRA_SITE}/rest/api/3/issue", json={"key": "QA-1"})

        create_issue(_jira_config(), _report(), _context())

        request = httpx_mock.get_requests()[-1]
        fields = httpx.Response(200, content=request.content).json()["fields"]
        assert set(fields) == {"project", "summary", "description", "issuetype", "labels"}
        assert "priority" not in fields
        assert "severity-high" in fields["labels"]
        assert "qa-agent-scripted" in fields["labels"]

    def test_exploratory_findings_are_labelled_as_such(self, httpx_mock):
        httpx_mock.add_response(url=f"{_JIRA_SITE}/rest/api/3/issue", json={"key": "QA-1"})

        create_issue(_jira_config(), _report(), _context(source_kind="exploratory"))

        request = httpx_mock.get_requests()[-1]
        labels = httpx.Response(200, content=request.content).json()["fields"]["labels"]
        assert "qa-agent-exploratory" in labels

    def test_a_label_with_a_space_raises_before_the_request(self, httpx_mock):
        """Jira 400s the whole create over a space in a label, so this is
        raised locally as the error the tracker would have returned —
        which keeps it inside `_export`'s TrackerError handler, costing
        one group rather than aborting the run's whole export. Not an
        assert, which `python -O` would strip.

        `httpx_mock` is requested but left unstubbed on purpose: a
        regression here has to fail as a raise that did not happen, never
        as a real request to a real Jira site.
        """
        with pytest.raises(TrackerError) as exc:
            create_issue(_jira_config(), _report(), _context(extra_labels=("needs triage",)))

        assert "must not contain a space" in str(exc.value)
        assert httpx_mock.get_requests() == []

    def test_also_observed_and_supersede_note_reach_the_body(self, httpx_mock):
        """Nothing is ever appended to a ticket afterwards, so this list is
        its whole record of how often the defect was seen."""
        httpx_mock.add_response(url=f"{_JIRA_SITE}/rest/api/3/issue", json={"key": "QA-1"})

        create_issue(
            _jira_config(),
            _report(),
            _context(
                also_observed=("Expired card — run 14, 2026-08-04 11:20 UTC",),
                superseded_key="QA-99",
            ),
        )

        request = httpx_mock.get_requests()[-1]
        body = request.content.decode()
        assert "Also observed as" in body
        assert "run 14, 2026-08-04 11:20 UTC" in body
        assert "QA-99" in body

    def test_transport_failure_raises_tracker_error_not_httpx(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("no route"))
        with pytest.raises(TrackerUnavailableError):
            create_issue(_jira_config(), _report(), _context())

    def test_timeout_raises_tracker_error(self, httpx_mock):
        httpx_mock.add_exception(httpx.ReadTimeout("slow"))
        with pytest.raises(TrackerUnavailableError):
            create_issue(_jira_config(), _report(), _context())

    def test_forbidden_raises_tracker_error(self, httpx_mock):
        httpx_mock.add_response(url=f"{_JIRA_SITE}/rest/api/3/issue", status_code=403)
        with pytest.raises(TrackerError) as exc:
            create_issue(_jira_config(), _report(), _context())
        assert not isinstance(exc.value, TrackerUnavailableError)

    def test_accepted_without_a_key_raises(self, httpx_mock):
        """A create that reports success but names nothing would otherwise
        persist a receipt pointing at no ticket."""
        httpx_mock.add_response(url=f"{_JIRA_SITE}/rest/api/3/issue", json={})
        with pytest.raises(TrackerError):
            create_issue(_jira_config(), _report(), _context())


class TestGitHubCreateIssue:
    def test_files_a_markdown_body_and_returns_the_number(self, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/acme/shop/issues",
            json={"number": 7, "html_url": "https://github.com/acme/shop/issues/7"},
            status_code=201,
        )

        ref = create_issue(_github_config(), _report(), _context())

        payload = httpx.Response(200, content=httpx_mock.get_requests()[-1].content).json()
        assert set(payload) == {"title", "body", "labels"}
        assert isinstance(payload["body"], str)
        assert "### Steps to reproduce" in payload["body"]
        assert ref.key == "7"
        assert ref.url == "https://github.com/acme/shop/issues/7"

    def test_accepted_without_a_number_raises(self, httpx_mock):
        httpx_mock.add_response(url="https://api.github.com/repos/acme/shop/issues", json={})
        with pytest.raises(TrackerError):
            create_issue(_github_config(), _report(), _context())


class TestRedaction:
    def test_removes_env_values_from_the_finding_text(self, httpx_mock):
        """The guard lives inside this module rather than at the call
        sites, because a ticket is the one artifact that leaves the app."""
        httpx_mock.add_response(
            url="https://api.github.com/repos/acme/shop/issues", json={"number": 1}
        )

        create_issue(
            _github_config(),
            _report(
                actual="Login failed for admin with password s3cr3t-passw0rd",
                steps_to_reproduce="Sign in as admin / s3cr3t-passw0rd",
            ),
            _context(secrets={"QA_PASSWORD": "s3cr3t-passw0rd"}),
        )

        body = httpx_mock.get_requests()[-1].content.decode()
        assert "s3cr3t-passw0rd" not in body
        # The variable's own name, not a blanking placeholder: a reader can
        # act on "which credential", and cannot on "something was here".
        assert "$QA_PASSWORD" in body

    def test_short_values_are_left_alone(self):
        """Rewriting a two-character value would gut ordinary prose while
        protecting nothing worth protecting."""
        text = "The port 80 page returned 500"
        assert redact(text, {"PORT": "80"}) == text

    def test_longest_value_wins_when_one_contains_another(self):
        redacted = redact("token=abcdef123456", {"SHORT": "abcdef", "LONG": "abcdef123456"})
        assert "abcdef" not in redacted
        assert redacted == "token=$LONG"

    def test_empty_text_is_returned_unchanged(self):
        assert redact("", {"QA_PASSWORD": "s3cr3t-passw0rd"}) == ""

    def test_two_names_sharing_a_value_resolve_deterministically(self):
        """Otherwise the same stderr redacts differently between runs."""
        secrets = {"BETA": "shared-value", "ALPHA": "shared-value"}

        assert redact("x shared-value y", secrets) == "x $ALPHA y"

    def test_each_value_becomes_its_own_name(self):
        secrets = {"BASE_URL": "https://staging.example.com", "QA_PASSWORD": "hunter2secret"}

        redacted = redact("GET https://staging.example.com/login as admin/hunter2secret", secrets)

        assert redacted == "GET $BASE_URL/login as admin/$QA_PASSWORD"


class TestIssueIsOpen:
    def test_jira_open_status_category(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{_JIRA_SITE}/rest/api/3/issue/QA-1?fields=status",
            json={"fields": {"status": {"statusCategory": {"key": "indeterminate"}}}},
        )
        assert issue_is_open(_jira_config(), "QA-1") is True

    def test_jira_done_status_category(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{_JIRA_SITE}/rest/api/3/issue/QA-1?fields=status",
            json={"fields": {"status": {"statusCategory": {"key": "done"}}}},
        )
        assert issue_is_open(_jira_config(), "QA-1") is False

    def test_github_open_and_closed(self, httpx_mock):
        httpx_mock.add_response(
            url="https://api.github.com/repos/acme/shop/issues/7", json={"state": "open"}
        )
        assert issue_is_open(_github_config(), "7") is True

        httpx_mock.reset()
        httpx_mock.add_response(
            url="https://api.github.com/repos/acme/shop/issues/7",
            json={"state": "closed", "state_reason": "not_planned"},
        )
        assert issue_is_open(_github_config(), "7") is False

    def test_missing_issue_reads_as_closed(self, httpx_mock):
        """Answering "open" for a ticket that is gone would attach a
        finding to nothing and lose it; answering "closed" costs a
        duplicate. The failure directions are not symmetric."""
        httpx_mock.add_response(
            url="https://api.github.com/repos/acme/shop/issues/7", status_code=404
        )
        assert issue_is_open(_github_config(), "7") is False

    def test_an_unexpected_body_shape_reads_as_closed(self, httpx_mock):
        """The contract is "resolve every doubt to False", so the handler
        catches everything — a body that is a list, not an object, would
        otherwise escape and abort the caller's whole export over one
        state check."""
        httpx_mock.add_response(
            url=f"{_JIRA_SITE}/rest/api/3/issue/QA-1?fields=status", json=["unexpected"]
        )
        assert issue_is_open(_jira_config(), "QA-1") is False

    def test_transport_error_reads_as_closed_and_does_not_raise(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("no route"))
        assert issue_is_open(_github_config(), "7") is False

    def test_malformed_config_does_not_raise(self):
        assert issue_is_open(_github_config(target="shop"), "7") is False


class TestAttachScreenshot:
    def test_uploads_with_the_xsrf_header(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{_JIRA_SITE}/rest/api/3/issue/QA-1/attachments", json=[{"id": "1"}]
        )

        attach_screenshot(_jira_config(), "QA-1", b"\x89PNG", "finding-1.png")

        request = httpx_mock.get_requests()[-1]
        assert request.headers["X-Atlassian-Token"] == "no-check"

    def test_swallows_a_server_error(self, httpx_mock):
        """A missing image costs evidence; raising here would cost the
        ticket the evidence belongs to."""
        httpx_mock.add_response(
            url=f"{_JIRA_SITE}/rest/api/3/issue/QA-1/attachments", status_code=500
        )
        attach_screenshot(_jira_config(), "QA-1", b"\x89PNG", "finding-1.png")

    def test_swallows_a_transport_error(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("no route"))
        attach_screenshot(_jira_config(), "QA-1", b"\x89PNG", "finding-1.png")

    def test_github_is_a_no_op(self, httpx_mock):
        """GitHub has no issue attachment API — silence, not a failure."""
        attach_screenshot(_github_config(), "7", b"\x89PNG", "finding-1.png")
        assert httpx_mock.get_requests() == []
