"""Transport to an external issue tracker — Jira or GitHub Issues.

Deliberately the only module that speaks HTTP to a tracker, and
deliberately DB-free: it takes plain values in and hands ``IssueRef``\\ s
back, so nothing here can read a row, write a receipt, or decide *whether*
a finding should be filed.  That decision lives in
``services/finding_export.py``; the grouping that precedes it lives in
``services/finding_dedup.py``.

Two properties are load-bearing:

* **Redaction happens here**, inside :func:`create_issue`, rather than at
  the call sites.  A ticket is the one artifact in this application that
  leaves it, so the guard belongs at the exit rather than at each of the
  places that walks toward it.
* **The client is synchronous.**  Routes reach it through
  ``asyncio.to_thread`` (the treatment LLM calls already get) and worker
  tasks call it directly.  A sync client also never needs an event loop,
  which is what keeps it safe to call from the exploratory task — where
  Playwright's sync API forbids one in the same thread.
"""

import base64
import json
import logging
import ssl
from dataclasses import dataclass, field

import certifi
import httpx

from backend.config import ISSUE_TRACKER_TIMEOUT
from backend.models.database import IssueTrackerProvider

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger(__name__)

_GITHUB_API_ROOT = "https://api.github.com"

# Jira's status *categories* are the only cross-instance vocabulary for
# "is this done" — the statuses themselves are per-workflow and can be
# named anything.  Everything outside this set reads as open, so an exotic
# workflow costs a duplicate ticket rather than a lost finding.
_JIRA_DONE_CATEGORY_KEYS = {"done", "completed"}

# Shortest environment value worth redacting from ticket text.  Below it
# the replacement does more damage than the leak: a two-character value
# ("80", "qa") occurs inside ordinary prose constantly, and a bug report
# with every other word blanked is not a bug report.  Real credentials
# comfortably clear this; ports and short usernames do not, and those are
# not what the guard is for.
_MIN_REDACTABLE_LENGTH = 6

_REDACTION_PLACEHOLDER = "***"


# ── Public shapes ─────────────────────────────────────────────────────


class TrackerError(Exception):
    """The tracker refused, or could not be reached.

    Raised in place of every ``httpx`` error, so no caller has to know
    which HTTP library is underneath.
    """


class TrackerUnavailableError(TrackerError):
    """Network failure, timeout, or a 5xx — the request never got an answer.

    Split from its parent so the config route can answer 502 for "not
    reachable" and 422 for "reachable, and it said no": the first is
    nobody's fault and worth retrying, the second is something the user
    must fix before saving.
    """


@dataclass(frozen=True)
class TrackerConfig:
    """Everything one outbound request needs, with a **plaintext** token.

    Deliberately not ``IssueTrackerConfig`` itself: that row stores the
    token Fernet-encrypted, and a module that accepted the row directly
    would happily send the ciphertext as a credential and report the
    resulting 401 as bad credentials.  Making the caller build this is
    what forces the decryption to be a decision rather than an omission.
    """

    provider: str
    target: str  # Jira project key | "owner/repo"
    api_token: str
    base_url: str | None = None  # Jira site root
    account_email: str | None = None  # Jira Basic-auth user
    issue_type: str | None = None  # Jira issue type name


@dataclass(frozen=True)
class IssueRef:
    """A filed ticket: the key a human quotes, and the URL they open."""

    key: str  # "QA-142" | "7"
    url: str  # absolute


@dataclass(frozen=True)
class FindingReport:
    """The finding as a ticket sees it — the shared seven fields.

    A dataclass rather than ``FindingBase`` because the response model
    also carries the tracker receipt, and a report that arrives holding
    its own issue key invites exactly the confusion this module should
    not have to guard against.
    """

    finding_type: str
    severity: str
    title: str
    steps_to_reproduce: str  # newline-joined
    expected: str
    actual: str
    environment: str | None = None


@dataclass(frozen=True)
class FindingContext:
    """Where the finding came from, and what else it stands for.

    ``secret_values`` has no default on purpose.  It is the input to the
    redaction pass, and a caller that forgot it would file a ticket that
    looks perfectly fine and quietly carries a test credential.  Making
    it required turns that omission into a ``TypeError`` at the call
    site.
    """

    sprint_name: str
    requirement_name: str
    run_label: str  # "Scripted run 14" | "Exploratory run 3"
    source_label: str  # test-case title | charter text
    source_kind: str  # "scripted" | "exploratory" — selects the label
    secret_values: frozenset[str]
    # The other findings this ticket stands for: title, run, and time
    # each. Load-bearing rather than decorative — nothing is ever appended
    # to a ticket afterwards, so this list is its entire record of how
    # often and when the defect was seen.
    also_observed: tuple[str, ...] = ()
    # Set when the ticket this finding matched had already been closed:
    # the new ticket back-references it rather than silently reopening a
    # decision somebody made.
    superseded_key: str | None = None
    # Cosmetic only — every ticket carries these plus a severity label.
    extra_labels: tuple[str, ...] = field(default=())


# ── Redaction ─────────────────────────────────────────────────────────


def redact(text: str, secret_values: frozenset[str] | set[str]) -> str:
    """Blank out test-environment credentials occurring in *text*.

    Substring matching, unlike the exploratory action log's exact match
    on a whole tool argument: a finding's ``actual`` is prose, and a
    credential leaks into it mid-sentence ("logged in as
    admin/hunter2…") rather than as the entire value.  The cost of
    substring matching is false positives, which is what
    ``_MIN_REDACTABLE_LENGTH`` bounds.

    Base URLs are excluded by the caller, not here: a bug report about a
    page has to be allowed to name the page.
    """
    if not text:
        return text
    # Longest first, so a value that contains another does not leave the
    # shorter one's replacement embedded in a half-redacted string.
    for value in sorted(secret_values, key=len, reverse=True):
        if value and len(value) >= _MIN_REDACTABLE_LENGTH:
            text = text.replace(value, _REDACTION_PLACEHOLDER)
    return text


def _redact_report(report: FindingReport, secrets: frozenset[str]) -> FindingReport:
    """Every free-text field of a report, redacted in one place."""
    return FindingReport(
        finding_type=report.finding_type,
        severity=report.severity,
        title=redact(report.title, secrets),
        steps_to_reproduce=redact(report.steps_to_reproduce, secrets),
        expected=redact(report.expected, secrets),
        actual=redact(report.actual, secrets),
        environment=redact(report.environment, secrets) if report.environment else None,
    )


# ── Labels ────────────────────────────────────────────────────────────


def _labels(report: FindingReport, context: FindingContext) -> list[str]:
    """The labels every filed ticket carries.

    Severity rides here rather than in Jira's ``priority`` field: priority
    is instance-configured and a wrong value 400s the whole create, while
    an unknown label is simply created.
    """
    kind = "exploratory" if context.source_kind == "exploratory" else "scripted"
    labels = ["qa-agent", f"qa-agent-{kind}", f"severity-{report.severity}"]
    labels.extend(context.extra_labels)
    for label in labels:
        # Jira silently rejects a whole create over a label with a space
        # in it, so this fails loudly at the one place that composes them.
        assert " " not in label, f"Issue label must not contain a space: {label!r}"
    return labels


def _steps(report: FindingReport) -> list[str]:
    """Reproduction steps as a list, tolerating a blank or single-line value."""
    return [line.strip() for line in report.steps_to_reproduce.splitlines() if line.strip()]


def _trailer(context: FindingContext) -> str:
    """Provenance line — what produced this ticket, in one string."""
    return (
        f"qa-agent finding · sprint: {context.sprint_name}"
        f" · requirement: {context.requirement_name}"
        f" · {context.run_label} · {context.source_label}"
    )


def _superseded_line(context: FindingContext) -> str | None:
    if not context.superseded_key:
        return None
    return (
        f"Previously filed as {context.superseded_key}, which has since been closed. "
        "Filed again because the defect was observed after that closure."
    )


# ── Renderers ─────────────────────────────────────────────────────────


def _adf_text(text: str) -> dict:
    return {"type": "text", "text": text}


def _adf_paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": [_adf_text(text)]}


def _adf_heading(text: str) -> dict:
    return {"type": "heading", "attrs": {"level": 3}, "content": [_adf_text(text)]}


def _adf_bullet_list(items: list[str]) -> dict:
    return {
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [_adf_paragraph(item)]} for item in items],
    }


def _render_adf(report: FindingReport, context: FindingContext) -> dict:
    """Atlassian Document Format body for the Jira v3 create API.

    Mandatory, not stylistic: v3 rejects a plain-string ``description``
    with a 400, so a "simpler" renderer files nothing at all.  Only three
    node types are used — paragraph, heading, bulletList — which is the
    whole grammar a bug report needs and the whole grammar this has to
    keep working.
    """
    content: list[dict] = []
    steps = _steps(report)
    if steps:
        content.append(_adf_heading("Steps to reproduce"))
        content.append(_adf_bullet_list(steps))
    content.append(_adf_heading("Expected"))
    content.append(_adf_paragraph(report.expected or "—"))
    content.append(_adf_heading("Actual"))
    content.append(_adf_paragraph(report.actual or "—"))
    if report.environment:
        content.append(_adf_heading("Environment"))
        content.append(_adf_paragraph(report.environment))
    if context.also_observed:
        content.append(_adf_heading("Also observed as"))
        content.append(_adf_bullet_list(list(context.also_observed)))
    superseded = _superseded_line(context)
    if superseded:
        content.append(_adf_paragraph(superseded))
    content.append(_adf_paragraph(_trailer(context)))
    return {"type": "doc", "version": 1, "content": content}


def _render_markdown(report: FindingReport, context: FindingContext) -> str:
    """GitHub issue body — the same sections as the ADF renderer."""
    parts: list[str] = []
    steps = _steps(report)
    if steps:
        parts.append("### Steps to reproduce\n" + "\n".join(f"- {step}" for step in steps))
    parts.append(f"### Expected\n{report.expected or '—'}")
    parts.append(f"### Actual\n{report.actual or '—'}")
    if report.environment:
        parts.append(f"### Environment\n{report.environment}")
    if context.also_observed:
        parts.append(
            "### Also observed as\n" + "\n".join(f"- {entry}" for entry in context.also_observed)
        )
    superseded = _superseded_line(context)
    if superseded:
        parts.append(superseded)
    parts.append(f"---\n{_trailer(context)}")
    return "\n\n".join(parts)


# ── HTTP plumbing ─────────────────────────────────────────────────────


def _jira_headers(config: TrackerConfig) -> dict[str, str]:
    raw = f"{config.account_email or ''}:{config.api_token}".encode()
    return {
        "Authorization": f"Basic {base64.b64encode(raw).decode()}",
        "Accept": "application/json",
        "User-Agent": "qa-agent",
    }


def _github_headers(config: TrackerConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "qa-agent",
    }


def _jira_base(config: TrackerConfig) -> str:
    if not config.base_url:
        raise TrackerError("A Jira site URL is required.")
    return config.base_url.rstrip("/")


def _github_repo(config: TrackerConfig) -> tuple[str, str]:
    parts = config.target.strip().strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise TrackerError(f"Expected a GitHub repository as 'owner/repo', got {config.target!r}.")
    return parts[0], parts[1]


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict | None = None,
    files: dict | None = None,
) -> httpx.Response:
    """One tracker request, with every transport failure mapped to our own.

    Returns the response whatever its status: callers differ on which
    codes are fatal (a 404 ends ``verify`` but merely answers
    ``issue_is_open``), so classification stays with them.  Only failures
    that produced *no* response raise from here.
    """
    try:
        with httpx.Client(verify=_SSL_CONTEXT, timeout=ISSUE_TRACKER_TIMEOUT) as client:
            return client.request(method, url, headers=headers, json=json_body, files=files)
    except httpx.TimeoutException:
        raise TrackerUnavailableError(
            f"The issue tracker did not respond within {ISSUE_TRACKER_TIMEOUT}s."
        ) from None
    except httpx.RequestError as exc:
        raise TrackerUnavailableError(f"Could not reach the issue tracker: {exc}") from exc


def _detail(response: httpx.Response, limit: int = 300) -> str:
    """A short, safe excerpt of an error body for the user-facing message."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:limit].strip()
    if isinstance(payload, dict):
        for key in ("errorMessages", "message", "errors"):
            value = payload.get(key)
            if value:
                return json.dumps(value)[:limit]
    return json.dumps(payload)[:limit]


def _raise_for_status(response: httpx.Response, action: str) -> None:
    """Turn a non-2xx into the right ``TrackerError`` flavour."""
    if response.is_success:
        return
    detail = _detail(response)
    if response.status_code >= 500:
        raise TrackerUnavailableError(
            f"The issue tracker returned {response.status_code} while {action}. {detail}".strip()
        )
    if response.status_code in (401, 403):
        raise TrackerError(
            f"The issue tracker rejected the credentials while {action} "
            f"({response.status_code}). {detail}".strip()
        )
    raise TrackerError(
        f"The issue tracker refused while {action} ({response.status_code}). {detail}".strip()
    )


# ── verify ────────────────────────────────────────────────────────────


def _verify_jira(config: TrackerConfig) -> str:
    base = _jira_base(config)
    headers = _jira_headers(config)

    response = _request("GET", f"{base}/rest/api/3/myself", headers=headers)
    _raise_for_status(response, "checking the Jira credentials")

    response = _request("GET", f"{base}/rest/api/3/project/{config.target}", headers=headers)
    if response.status_code == 404:
        raise TrackerError(
            f"Jira project {config.target!r} was not found, or this account cannot see it."
        )
    _raise_for_status(response, "looking up the Jira project")
    project_name = response.json().get("name") or config.target

    if not config.issue_type:
        raise TrackerError("A Jira issue type is required (for example 'Bug').")
    response = _request(
        "GET",
        f"{base}/rest/api/3/issue/createmeta"
        f"?projectKeys={config.target}&expand=projects.issuetypes",
        headers=headers,
    )
    _raise_for_status(response, "reading the Jira issue types")
    available = {
        issue_type.get("name")
        for project in response.json().get("projects", [])
        for issue_type in project.get("issuetypes", [])
    }
    if config.issue_type not in available:
        known = ", ".join(sorted(name for name in available if name)) or "none"
        raise TrackerError(
            f"Issue type {config.issue_type!r} does not exist in project {config.target}. "
            f"Available: {known}."
        )
    return f"{project_name} ({config.target})"


def _verify_github(config: TrackerConfig) -> str:
    owner, repo = _github_repo(config)
    response = _request(
        "GET", f"{_GITHUB_API_ROOT}/repos/{owner}/{repo}", headers=_github_headers(config)
    )
    if response.status_code == 404:
        raise TrackerError(f"Repository {owner}/{repo} was not found, or this token cannot see it.")
    _raise_for_status(response, "looking up the repository")
    data = response.json()
    if not data.get("has_issues"):
        raise TrackerError(f"Issues are disabled on {owner}/{repo}. Enable them and try again.")
    # Deliberately *not* checking permissions.push: a fine-grained token
    # can hold Issues:write without push, and requiring push would reject
    # exactly the tokens a careful user creates for this. A token that
    # cannot write surfaces at the first create, as tracker_error.
    return data.get("full_name") or f"{owner}/{repo}"


def verify(config: TrackerConfig) -> str:
    """Confirm the credentials and target are usable; return a display label.

    Called on every save — first connect and every edit alike — and never
    on a schedule.  It runs where the user is present to act on the
    answer; a background re-check would need somewhere to put a result
    nobody is waiting for.

    Raises ``TrackerError`` when the tracker answered "no",
    ``TrackerUnavailableError`` when it did not answer.
    """
    if config.provider == IssueTrackerProvider.JIRA:
        return _verify_jira(config)
    if config.provider == IssueTrackerProvider.GITHUB:
        return _verify_github(config)
    raise TrackerError(f"Unknown issue tracker provider: {config.provider!r}")


# ── create_issue ──────────────────────────────────────────────────────


def _create_jira(config: TrackerConfig, report: FindingReport, context: FindingContext) -> IssueRef:
    base = _jira_base(config)
    payload = {
        "fields": {
            "project": {"key": config.target},
            "summary": report.title,
            "description": _render_adf(report, context),
            "issuetype": {"name": config.issue_type},
            "labels": _labels(report, context),
        }
    }
    response = _request(
        "POST", f"{base}/rest/api/3/issue", headers=_jira_headers(config), json_body=payload
    )
    _raise_for_status(response, "creating the Jira issue")
    key = response.json().get("key")
    if not key:
        raise TrackerError("Jira accepted the issue but returned no key.")
    return IssueRef(key=key, url=f"{base}/browse/{key}")


def _create_github(
    config: TrackerConfig, report: FindingReport, context: FindingContext
) -> IssueRef:
    owner, repo = _github_repo(config)
    payload = {
        "title": report.title,
        "body": _render_markdown(report, context),
        "labels": _labels(report, context),
    }
    response = _request(
        "POST",
        f"{_GITHUB_API_ROOT}/repos/{owner}/{repo}/issues",
        headers=_github_headers(config),
        json_body=payload,
    )
    _raise_for_status(response, "creating the GitHub issue")
    data = response.json()
    number = data.get("number")
    if number is None:
        raise TrackerError("GitHub accepted the issue but returned no number.")
    return IssueRef(key=str(number), url=data.get("html_url") or "")


def create_issue(config: TrackerConfig, report: FindingReport, context: FindingContext) -> IssueRef:
    """File one ticket for *report*; raise ``TrackerError`` if it did not land.

    Redaction runs here, on the way out, so no call site can bypass it.

    This is the one operation in the module that is allowed to raise on
    ordinary failure: everything else degrades, because a failed state
    check or a missing screenshot costs a detail, whereas a create that
    silently did nothing would leave a finding claiming to be filed.
    """
    safe_report = _redact_report(report, context.secret_values)
    safe_context = FindingContext(
        sprint_name=context.sprint_name,
        requirement_name=context.requirement_name,
        run_label=context.run_label,
        source_label=redact(context.source_label, context.secret_values),
        source_kind=context.source_kind,
        secret_values=context.secret_values,
        also_observed=tuple(
            redact(entry, context.secret_values) for entry in context.also_observed
        ),
        superseded_key=context.superseded_key,
        extra_labels=context.extra_labels,
    )
    if config.provider == IssueTrackerProvider.JIRA:
        return _create_jira(config, safe_report, safe_context)
    if config.provider == IssueTrackerProvider.GITHUB:
        return _create_github(config, safe_report, safe_context)
    raise TrackerError(f"Unknown issue tracker provider: {config.provider!r}")


# ── issue_is_open ─────────────────────────────────────────────────────


def issue_is_open(config: TrackerConfig, key: str) -> bool:
    """Whether *key* is still open — ``False`` on any doubt, never raising.

    The caller adopts an existing ticket when this is true and files a
    fresh one when it is false, so the failure directions are not
    symmetric: answering "closed" for a ticket that is really open costs
    a duplicate, while answering "open" for one that is really gone
    attaches a finding to nothing and loses it.  Every uncertain
    answer — 404, revoked token, tracker down — therefore resolves to
    ``False``.
    """
    try:
        if config.provider == IssueTrackerProvider.JIRA:
            response = _request(
                "GET",
                f"{_jira_base(config)}/rest/api/3/issue/{key}?fields=status",
                headers=_jira_headers(config),
            )
            if not response.is_success:
                return False
            category = (
                response.json()
                .get("fields", {})
                .get("status", {})
                .get("statusCategory", {})
                .get("key")
            )
            return category not in _JIRA_DONE_CATEGORY_KEYS
        if config.provider == IssueTrackerProvider.GITHUB:
            owner, repo = _github_repo(config)
            response = _request(
                "GET",
                f"{_GITHUB_API_ROOT}/repos/{owner}/{repo}/issues/{key}",
                headers=_github_headers(config),
            )
            if not response.is_success:
                return False
            # `state_reason` is deliberately not read: nothing branches on
            # *why* an issue was closed, only on whether it is.
            return response.json().get("state") == "open"
    except (TrackerError, ValueError) as exc:
        logger.warning("Could not check whether issue %s is open: %s", key, exc)
    return False


# ── attach_screenshot ─────────────────────────────────────────────────


def attach_screenshot(config: TrackerConfig, key: str, png: bytes, filename: str) -> None:
    """Attach a screenshot to *key*; never raises.

    Jira only — GitHub has no attachment API for issues, so this is a
    no-op there rather than a reported failure.  The whole operation is
    best-effort in the same spirit: a missing image costs evidence, and
    failing the export over it would cost the ticket.
    """
    if config.provider != IssueTrackerProvider.JIRA:
        return
    try:
        headers = _jira_headers(config)
        # Required by Jira for any multipart upload; without it the
        # request is rejected as a suspected XSRF attempt.
        headers["X-Atlassian-Token"] = "no-check"
        response = _request(
            "POST",
            f"{_jira_base(config)}/rest/api/3/issue/{key}/attachments",
            headers=headers,
            files={"file": (filename, png, "image/png")},
        )
        if not response.is_success:
            logger.warning(
                "Could not attach a screenshot to %s: %s %s",
                key,
                response.status_code,
                _detail(response),
            )
    except TrackerError as exc:
        logger.warning("Could not attach a screenshot to %s: %s", key, exc)
