"""The half of a CI/CD export that we author, and the gate over the half we don't.

Three responsibilities, all pure functions over text:

**The deterministic block.**  ``qa_job_steps`` (GitHub Actions) and
``jenkins_stage_block`` (Jenkins) emit the install-and-run sequence
themselves rather than asking the model for it.  It is the same sequence
``script_runner`` performs — install, install browsers, ``python <file>``
per script — and it is built entirely from *names*, so no environment value
is ever in scope in the function that writes it.

**The layout.**  ``script_files`` maps cases to repository paths.  The row
id is appended unconditionally at both levels, so two cases that slugify
alike cannot silently become one file, and two requirements that slugify
alike cannot share a directory.  A rule with no exception cannot have an
untested exception.

**The gate.**  ``validate`` is the one place model output becomes a
filesystem effect.  It refuses rather than rewrites: raising costs a retry,
while quietly editing the model's output — or the team's own CI file —
ships a change nobody reviewed.

**A commit here is not a credential exit**, and there is deliberately no
containment check to make it one.  Both of its inputs are value-free by
construction: ``cicd_context`` hands the model the variable/secret *name*
split and never a value, and a cached script had its captured output
rewritten to ``$NAME`` before the diagnosis call that produced it.  Closing
the leak where a value could actually enter — ``llm.diagnose_and_fix_script``
— protects every consumer of a cached script, where a check here would only
have caught it on the way to GitHub, and only by reading text this system
wrote itself.
"""

import io
import logging
import re
from collections.abc import Sequence

from backend.models.database import TestCase
from backend.services import jenkins_text
from backend.services.ci_introspect import WorkflowEditError, _round_trip_yaml, _safe_yaml

logger = logging.getLogger(__name__)

EXPORT_ROOT = "qa-agent-tests"

# Where a model-authored path may land. Wider than `originals` on purpose —
# a new workflow file has no pre-existing counterpart to be a key of.
_ALLOWED_PATH_PATTERNS = (
    re.compile(r"^\.github/workflows/[^/]+\.ya?ml$"),
    re.compile(r"^\.github/actions/[^\0]+$"),
    re.compile(r"^Jenkinsfile$"),
    re.compile(r"^Jenkinsfile\.[A-Za-z0-9._-]+$"),
    re.compile(r"^ci/[^\0]+$"),
    re.compile(rf"^{EXPORT_ROOT}/[^\0]+$"),
)

_SLUG_MAX = 40

# GitHub Actions secret names: letters, digits and underscores only, and
# `GITHUB_` is reserved. A Jenkins credential id lives in a different
# namespace with different rules — hence two sanitizers, not one.
_ACTIONS_NAME_RE = re.compile(r"[^A-Za-z0-9_]")
_JENKINS_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")

_ACTIONS_REFERENCE_RE = re.compile(r"\$\{\{\s*(vars|secrets)\.([A-Za-z_]\w*)\s*\}\}")
_JENKINS_REFERENCES = (
    re.compile(r"env\.([A-Za-z_]\w*)"),
    re.compile(r"credentialsId:\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"credentials\(\s*['\"]([^'\"]+)['\"]\s*\)"),
)

# The libraries a generated script may import — the same closed set
# `script_runner` runs against, so CI installs exactly what the script needs.
_SCRIPT_REQUIREMENTS = ("playwright", "requests", "faker", "psycopg2-binary")


class CicdValidationError(ValueError):
    """Model output that must not become a filesystem effect.

    Raising rather than repairing is the whole point: a retry costs one
    generation, while a silent repair ships a change nobody reviewed into
    a repository we do not own.
    """


# ── Layout ────────────────────────────────────────────────────────────


def slugify(text: str, fallback: str) -> str:
    """A path-safe slug, with a stated fallback and a stated truncation.

    Lowercased, ASCII alphanumerics kept, every other run collapsed to a
    single ``-``, stripped, capped at 40 characters.  ``fallback`` covers
    the reachable empty case — a title written entirely in non-ASCII
    characters slugifies to nothing, and without this the path becomes
    ``_47.py``: legal, ugly, and silently different from what every test
    asserts.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:_SLUG_MAX].strip("-")
    return slug or fallback


def script_path(case: TestCase) -> str:
    """Where one case's script is committed.

    ``qa-agent-tests/<req-slug>_<requirement_id>/<case-slug>_<case_id>.py``

    The id is appended **unconditionally** at both levels rather than only
    on collision.  ``TestCase.id`` is a primary key, so the leaf can never
    collide once it carries one; the directory needs the same treatment
    because ``<req-slug>`` alone is not an identity, and two requirements
    that slugify alike would otherwise land unrelated scripts in one
    directory where no reviewer can tell them apart.
    """
    plan = case.test_plan
    requirement = plan.requirement if plan is not None else None
    requirement_id = requirement.id if requirement is not None else 0
    requirement_name = requirement.name if requirement is not None else ""
    directory = f"{slugify(requirement_name, 'requirement')}_{requirement_id}"
    filename = f"{slugify(case.title, 'case')}_{case.id}.py"
    return f"{EXPORT_ROOT}/{directory}/{filename}"


def script_files(cases: Sequence[TestCase]) -> dict[str, str]:
    """``{path: script}`` for every case being exported.

    Content is the cached script **verbatim** — this is the one thing in
    the export the model never touches, and there is no field in
    ``CicdIntegrationResult`` that could carry a replacement.
    """
    return {script_path(case): case.script or "" for case in cases}


# ── The deterministic block ───────────────────────────────────────────


def sanitize_actions_name(name: str) -> str:
    """A GitHub Actions variable/secret name derived from an env var name.

    Actions permits only ``[A-Za-z0-9_]`` and reserves the ``GITHUB_``
    prefix, so a name that violates either is rewritten and the mapping
    goes in the PR trailer.
    """
    cleaned = _ACTIONS_NAME_RE.sub("_", name).upper().strip("_") or "QA_VAR"
    if cleaned.startswith("GITHUB_"):
        cleaned = f"QA_{cleaned}"
    if cleaned[0].isdigit():
        cleaned = f"QA_{cleaned}"
    return cleaned


def sanitize_jenkins_id(name: str) -> str:
    """A Jenkins credential id derived from an env var name.

    Deliberately **not** a reuse of the Actions sanitizer: Jenkins ids
    allow dots and hyphens and reserve nothing, so one function cannot
    serve both namespaces without over-mangling one of them.  A
    ``GITHUB_``-prefixed name is perfectly legal here.
    """
    cleaned = _JENKINS_ID_RE.sub("_", name).strip("_") or "qa_var"
    if cleaned[0].isdigit():
        cleaned = f"qa_{cleaned}"
    return cleaned


def _install_commands(repo_install: Sequence[str] | None) -> list[str]:
    """The repo's own install first, then a top-up of what it did not supply.

    Reusing the repository's install step is what makes the job look like
    it belongs there; the top-up is what makes it actually run.
    """
    commands = [command for command in (repo_install or []) if command.strip()]
    supplied = " ".join(commands)
    missing = [name for name in _SCRIPT_REQUIREMENTS if name.split("-")[0] not in supplied]
    if missing:
        commands.append(f"pip install {' '.join(missing)}")
    commands.append("playwright install --with-deps chromium")
    return commands


def qa_job_steps(
    script_paths: Sequence[str],
    variable_names: Sequence[str],
    secret_names: Sequence[str],
    repo_install: Sequence[str] | None = None,
) -> list[dict]:
    """The GitHub Actions step list, emitted by us rather than by the model.

    Built from **names only** — no environment value is in scope in this
    function, which is exactly why the containment gate must not read its
    output (a sprint with ``BROWSER=chromium`` would otherwise hard-refuse
    on ``playwright install … chromium``, unfixably and undiagnosably).

    The run sequence mirrors ``script_runner``: one ``python <file>`` per
    script, no test framework in between, because that is how these scripts
    were verified.
    """
    env = {name: f"${{{{ vars.{sanitize_actions_name(name)} }}}}" for name in variable_names}
    env.update({name: f"${{{{ secrets.{sanitize_actions_name(name)} }}}}" for name in secret_names})

    steps: list[dict] = [{"uses": "actions/checkout@v4"}]
    for command in _install_commands(repo_install):
        steps.append({"run": command})
    for path in script_paths:
        step: dict = {"name": f"QA: {path}", "run": f"python {path}"}
        if env:
            step["env"] = dict(env)
        steps.append(step)
    return steps


def render_job_steps(steps: Sequence[dict]) -> str:
    """The Actions step list as YAML text, for the prompt to show the model.

    Rendered rather than described, because rule 3 of the system prompt
    tells the model to integrate these steps as given — it needs the actual
    bytes, not a summary of them.
    """
    stream = io.StringIO()
    _round_trip_yaml().dump(list(steps), stream)
    return stream.getvalue()


def jenkins_stage_block(
    script_paths: Sequence[str],
    variable_names: Sequence[str],
    secret_names: Sequence[str],
    repo_install: Sequence[str] | None = None,
) -> str:
    """The Jenkins counterpart, so the deterministic-block rule holds for both.

    Without this, Jenkins would have had no block of ours at all and the
    model would be authoring the install and run steps itself — the one
    thing this design does not delegate.
    """
    lines = ["stage('QA Agent E2E') {"]
    if variable_names:
        lines.append("  environment {")
        for name in variable_names:
            lines.append(f'    {sanitize_jenkins_id(name).upper()} = "${{env.{name}}}"')
        lines.append("  }")
    lines.append("  steps {")

    indent = "    "
    if secret_names:
        bindings = ", ".join(
            f"string(credentialsId: '{sanitize_jenkins_id(name)}', variable: '{name}')"
            for name in secret_names
        )
        lines.append(f"    withCredentials([{bindings}]) {{")
        indent = "      "
    for command in _install_commands(repo_install):
        lines.append(f"{indent}sh '{command}'")
    for path in script_paths:
        lines.append(f"{indent}sh 'python {path}'")
    if secret_names:
        lines.append("    }")
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines)


# ── The gate ──────────────────────────────────────────────────────────


def _path_allowed(path: str) -> bool:
    if not path or path.startswith("/") or ".." in path.split("/"):
        return False
    return any(pattern.match(path) for pattern in _ALLOWED_PATH_PATTERNS)


def _check_actions_structure(path: str, content: str) -> None:
    """A workflow must parse and carry both a trigger key and ``jobs``.

    A structural **floor**, not a claim the workflow works — nothing here
    can establish that, and the pull request is what does.
    """
    try:
        doc = _safe_yaml().load(content)
    except Exception as exc:
        raise CicdValidationError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise CicdValidationError(f"{path} is not a YAML mapping")
    # YAML 1.1 parses a bare `on:` as the boolean True — accept both keys.
    if "on" not in doc and True not in doc:
        raise CicdValidationError(f"{path} declares no triggers ('on')")
    if not isinstance(doc.get("jobs"), dict):
        raise CicdValidationError(f"{path} declares no jobs")


def _check_jenkins_structure(path: str, content: str) -> None:
    problems = jenkins_text.floor_check(content)
    if problems:
        raise CicdValidationError(f"{path} is not a usable Jenkinsfile: {'; '.join(problems)}")


def _referenced_names(text: str, provider_is_actions: bool) -> set[str]:
    """Every CI variable/secret name the text refers to.

    Provider-dispatched because the syntaxes share nothing: ``${{ … }}``
    never appears in a Jenkinsfile, so applying the Actions pattern to
    Groovy matches nothing and passes vacuously — which is precisely how
    unvalidated Jenkins output would ship.
    """
    if provider_is_actions:
        return {match.group(2) for match in _ACTIONS_REFERENCE_RE.finditer(text)}
    names: set[str] = set()
    for pattern in _JENKINS_REFERENCES:
        names.update(match.group(1) for match in pattern.finditer(text))
    return names


def validate(
    result,
    allowed_names: Sequence[str],
    originals: dict[str, str],
    *,
    provider_is_actions: bool,
) -> list[str]:
    """Check model output before anything is written. Returns dropped paths.

    Four checks, three of them provider-dispatched:

    1. **path allowlist** — the one place model output becomes a filesystem
       effect. Offending files are dropped and named in the PR trailer
       rather than failing the whole export;
    2. **host-edit target** — a ``host_edit`` naming a file this export did
       not fetch is refused. ``originals`` holds only the CI files fetched
       this run, while the allowlist is far wider, so this is reachable;
       without it the splice is a ``KeyError`` that burns three retries, or
       a silent degradation to check-everything;
    3. **structural floor** — parses, and carries the minimum a CI file of
       that kind must carry;
    4. **reference resolution** — every ``vars``/``secrets``/``credentials``
       name resolves to one we supplied, or the job runs with a silently
       blank variable and fails for reasons unrelated to the product.

    There is deliberately **no secret-containment check**.  Neither input to
    a commit can carry an environment value: the model is shown the
    variable/secret *names* and never a value, and a cached script had its
    captured output rewritten to ``$NAME`` before the diagnosis call that
    produced it (``llm.diagnose_and_fix_script``).  A check here would have
    had to read text this system wrote itself — which can only produce false
    positives, and a false positive is unfixable by retry (every attempt
    regenerates the same text) and undiagnosable by construction (the error
    cannot name the value without leaking it).
    """
    allowed = set(allowed_names)
    dropped: list[str] = []
    checked: list[tuple[str, str]] = []

    for item in result.files:
        if not _path_allowed(item.path):
            dropped.append(item.path)
            logger.warning("CI/CD export: refusing path outside the allowlist: %s", item.path)
            continue
        checked.append((item.path, item.content))

    host_edit = getattr(result, "host_edit", None)
    if host_edit is not None:
        if host_edit.path not in originals:
            raise CicdValidationError(
                f"Host edit names {host_edit.path!r}, which this export did not fetch. "
                "Only a CI file read during this export may be edited."
            )
        checked.append((f"{host_edit.path} (added job)", host_edit.job_body))

    for path, content in checked:
        if path.endswith((".yml", ".yaml")) and provider_is_actions:
            _check_actions_structure(path, content)
        elif "Jenkinsfile" in path:
            _check_jenkins_structure(path, content)

        referenced = _referenced_names(content, provider_is_actions)
        unknown = sorted(referenced - allowed)
        if unknown:
            raise CicdValidationError(
                f"{path} references {', '.join(unknown)}, which the sprint does not define. "
                "Every variable and secret must be one of: " + ", ".join(sorted(allowed))
            )

    if host_edit is not None and provider_is_actions:
        _check_actions_job_body(host_edit)

    return dropped


def _check_actions_job_body(host_edit) -> None:
    """An added job must be a mapping carrying ``steps``.

    The Actions analogue of ``floor_check``: without it the splice writes a
    string or a list under ``jobs:`` and produces a workflow that fails to
    parse in the target repository.
    """
    try:
        body = _safe_yaml().load(host_edit.job_body)
    except Exception as exc:
        raise CicdValidationError(f"The added job is not valid YAML: {exc}") from exc
    if not isinstance(body, dict) or "steps" not in body:
        raise CicdValidationError("The added job must be a mapping carrying 'steps'")


def parse_job_body(job_body: str) -> dict:
    """The model's job fragment as a mapping, for ``add_job`` to splice in."""
    try:
        body = _safe_yaml().load(job_body)
    except Exception as exc:
        raise WorkflowEditError(f"The added job is not valid YAML: {exc}") from exc
    if not isinstance(body, dict):
        raise WorkflowEditError("The added job must be a YAML mapping")
    return body


# ── The PR body ───────────────────────────────────────────────────────


def pr_body(
    llm_prose: str,
    sprint_name: str,
    case_count: int,
    variable_names: Sequence[str],
    secret_names: Sequence[str],
    dropped: Sequence[str],
    *,
    provider_is_actions: bool,
    notes: str | None = None,
    extra_notices: Sequence[str] = (),
) -> str:
    """The model's prose plus the trailer we control.

    The trailer exists because the model cannot be the only source of
    "what must you do before this works".  It names the variables and
    secrets the team has to create — **names only** — and any path the gate
    refused, so a dropped file is visible rather than silently missing.
    """
    parts = [llm_prose.strip(), "", "---", "", "## Generated by QA Agent", ""]
    parts.append(f"Sprint: **{sprint_name}**")
    parts.append(f"Test scripts committed: **{case_count}**")
    parts.append("")

    if variable_names or secret_names:
        kind = "repository variables and secrets" if provider_is_actions else "credentials"
        parts.append(f"### Before this runs, create these {kind}")
        parts.append("")
        for name in variable_names:
            mapped = (
                sanitize_actions_name(name) if provider_is_actions else sanitize_jenkins_id(name)
            )
            suffix = f" (referenced as `{mapped}`)" if mapped != name else ""
            parts.append(f"- variable `{name}`{suffix}")
        for name in secret_names:
            mapped = (
                sanitize_actions_name(name) if provider_is_actions else sanitize_jenkins_id(name)
            )
            suffix = f" (referenced as `{mapped}`)" if mapped != name else ""
            parts.append(f"- secret `{name}`{suffix}")
        parts.append("")
        parts.append("Values are deliberately not included here — QA Agent never writes an")
        parts.append("environment value into a repository.")
    else:
        # Itself the useful signal: a suite with no environment target.
        parts.append("### No environment variables were defined for this sprint")
        parts.append("")
        parts.append("The generated job has no environment to point at. Check that the")
        parts.append("scripts can reach the application before enabling the schedule.")
    parts.append("")

    for notice in extra_notices:
        parts.append(f"> {notice}")
        parts.append("")

    if notes:
        parts.append("### Notes from generation")
        parts.append("")
        parts.append(notes.strip())
        parts.append("")

    if dropped:
        parts.append("### Files that were not written")
        parts.append("")
        parts.append("These paths fell outside what QA Agent may write, and were dropped:")
        parts.append("")
        for path in dropped:
            parts.append(f"- `{path}`")
        parts.append("")

    return "\n".join(parts).strip() + "\n"
