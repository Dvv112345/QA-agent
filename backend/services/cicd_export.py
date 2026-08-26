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
from collections.abc import Callable, Sequence

from backend.models.database import TestCase
from backend.services import jenkins_text
from backend.services.ci_introspect import WorkflowEditError, round_trip_yaml, safe_yaml

logger = logging.getLogger(__name__)

EXPORT_ROOT = "qa-agent-tests"

# Where a model-authored path may land. Wider than `originals` on purpose —
# a new workflow file has no pre-existing counterpart to be a key of.
#
# `qa-agent-tests/` is deliberately **absent**. The brainstorm framed this
# list as "every path the export may write" and included the script root on
# that reading, but the gate only ever runs over model output: our scripts
# reach the commit through `script_files()` and never pass through here. So
# the entry did nothing for them and only granted the model write access to
# the verified suite. Nothing needs it — a script is run as `python <file>`
# with no test framework, so there is no conftest, no `__init__.py` and no
# requirements file to place beside it.
_ALLOWED_PATH_PATTERNS = (
    re.compile(r"^\.github/workflows/[^/]+\.ya?ml$"),
    re.compile(r"^\.github/actions/[^\0]+$"),
    re.compile(r"^Jenkinsfile$"),
    re.compile(r"^Jenkinsfile\.[A-Za-z0-9._-]+$"),
    re.compile(r"^ci/[^\0]+$"),
)

_SLUG_MAX = 40

# GitHub Actions variable/secret names: letters, digits and underscores
# only, and `GITHUB_` is reserved. Jenkins has *two* further namespaces with
# different rules — a credential id, and an env var name referenced as
# `env.NAME` — hence three sanitizers, all reached through `reference_map`.
_ACTIONS_NAME_RE = re.compile(r"[^A-Za-z0-9_]")
_JENKINS_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")

_ACTIONS_REFERENCE_RE = re.compile(r"\$\{\{\s*(vars|secrets)\.([A-Za-z_]\w*)\s*\}\}")
_JENKINS_REFERENCES = (
    re.compile(r"env\.([A-Za-z_]\w*)"),
    re.compile(r"credentialsId:\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"credentials\(\s*['\"]([^'\"]+)['\"]\s*\)"),
)

# Jenkins populates these itself, so a stage may reference one without the
# sprint having defined anything. Without this, an ordinary
# `echo "${env.BUILD_NUMBER}"` is refused as an undefined variable and the
# export burns every retry on text that regenerates identically.
#
# **Not a closed set**, unlike Actions' `vars.`/`secrets.`: Jenkins' `env`
# namespace is extended by plugins (git supplies `GIT_*`, multibranch
# supplies `BRANCH_NAME`/`CHANGE_*`), so this is a pragmatic floor covering
# the core and the common plugins. The gate's real job is catching a name
# that looks like one of *ours* but is not — a typo'd `env.BAES_URL` — and
# that still fails here.
_JENKINS_BUILTINS = frozenset(
    {
        "BUILD_NUMBER",
        "BUILD_ID",
        "BUILD_DISPLAY_NAME",
        "BUILD_TAG",
        "BUILD_URL",
        "JOB_NAME",
        "JOB_BASE_NAME",
        "JOB_URL",
        "JENKINS_URL",
        "JENKINS_HOME",
        "EXECUTOR_NUMBER",
        "NODE_NAME",
        "NODE_LABELS",
        "WORKSPACE",
        "WORKSPACE_TMP",
        # Supplied by the git and multibranch plugins.
        "GIT_COMMIT",
        "GIT_BRANCH",
        "GIT_URL",
        "GIT_PREVIOUS_COMMIT",
        "GIT_PREVIOUS_SUCCESSFUL_COMMIT",
        "BRANCH_NAME",
        "CHANGE_ID",
        "CHANGE_TARGET",
        "CHANGE_BRANCH",
        "CHANGE_AUTHOR",
        "CHANGE_URL",
        "TAG_NAME",
    }
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


def _requirement_of(case: TestCase):
    """The requirement a case belongs to, or ``None``.

    The chain is walked in one place because three callers need it and a
    case whose plan or requirement was archived must read as absent rather
    than raise — the export lists cases long after the rows around them
    have moved.
    """
    plan = case.test_plan
    return plan.requirement if plan is not None else None


def requirement_name(case: TestCase) -> str:
    """The requirement's name, as the export copies it onto receipts and the PR."""
    requirement = _requirement_of(case)
    return requirement.name if requirement is not None else ""


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
    requirement = _requirement_of(case)
    requirement_id = requirement.id if requirement is not None else 0
    directory = f"{slugify(requirement_name(case), 'requirement')}_{requirement_id}"
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
    """A Jenkins **credential id** derived from an env var name.

    Deliberately **not** a reuse of the Actions sanitizer: Jenkins ids
    allow dots and hyphens and reserve nothing, so one function cannot
    serve both namespaces without over-mangling one of them.  A
    ``GITHUB_``-prefixed name is perfectly legal here.
    """
    cleaned = _JENKINS_ID_RE.sub("_", name).strip("_") or "qa_var"
    if cleaned[0].isdigit():
        cleaned = f"qa_{cleaned}"
    return cleaned


def sanitize_jenkins_env(name: str) -> str:
    """A Jenkins **environment variable** name derived from an env var name.

    Three sanitizers rather than two, because there are three namespaces
    and not two.  A Jenkins credential id is an arbitrary string
    (:func:`sanitize_jenkins_id`); a Jenkins env var name referenced as
    ``env.NAME`` has to be an identifier, so dots and hyphens — which the
    credential id keeps — must go here.  Deriving both from one function
    and upper-casing one of the results is what let the two drift apart.
    """
    cleaned = _ACTIONS_NAME_RE.sub("_", name).upper().strip("_") or "QA_VAR"
    if cleaned[0].isdigit():
        cleaned = f"QA_{cleaned}"
    return cleaned


def _namespace_map(names: Sequence[str], sanitize: Callable[[str], str]) -> dict[str, str]:
    """``{env var name: CI name}`` for **one** namespace, collisions broken apart.

    Every sanitizer is many-to-one — ``base_url`` and ``base.url`` both
    become ``BASE_URL`` — and nothing downstream could notice.  The block
    would emit one reference for two variables, the gate would see a name
    it had itself supplied and accept it, and the export would succeed with
    one of the two scripts silently reading the other's value.
    ``script_path`` has the same problem one layer down and settles it with
    ``TestCase.id``; a CI name has no id to carry, so a numeric suffix is
    the disambiguator.

    The order is deliberately **not** the one ``env_vars_json`` happens to
    be in.  A re-extraction or a hand edit reorders that dict, and an
    assignment that followed it would quietly swap two variables the team
    had already created in their CI — the same values, now feeding the
    wrong scripts, with nothing on screen changed.  Sorting fixes the
    assignment to the names themselves, and a name the CI system already
    accepts verbatim sorts first, because it is not the one that should be
    rewritten.
    """
    taken: set[str] = set()
    mapped: dict[str, str] = {}
    for name in sorted(names, key=lambda n: (sanitize(n) != n, n)):
        target = sanitize(name)
        candidate, suffix = target, 2
        while candidate in taken:
            candidate = f"{target}_{suffix}"
            suffix += 1
        taken.add(candidate)
        mapped[name] = candidate
    return mapped


def reference_map(
    variable_names: Sequence[str],
    secret_names: Sequence[str],
    *,
    provider_is_actions: bool,
) -> dict[str, str]:
    """``{env var name: the name the CI system carries it under}``.

    **The one place a CI-facing name is derived.**  The deterministic block
    emits these names, the prompt offers them to the model, the PR trailer
    explains them and the validation gate accepts them — four consumers
    that must agree exactly.  When each derived its own, they did not: the
    block emitted ``${{ vars.BASE_URL }}`` for an env var called
    ``base_url`` while the gate was handed ``base_url``, so the gate
    refused our own output and no retry could ever fix it (every attempt
    regenerates the same text).

    Variables and secrets are mapped separately because on Jenkins they
    land in different namespaces — an env var name against a credential id.
    On Actions both are ``[A-Za-z0-9_]`` and the two branches coincide.

    That separation is also why collisions are resolved **within** each
    group rather than across the pair: a variable and a secret that
    sanitize alike land in different stores (``vars.X`` and ``secrets.X``;
    an env name and a credential id) and are not in fact the same name.
    Rewriting one of them would invent a difference the CI system does not
    have.  Two *variables* that sanitize alike genuinely are one name, and
    those are what the suffix separates.
    """
    if provider_is_actions:
        mapped = _namespace_map(variable_names, sanitize_actions_name)
        mapped.update(_namespace_map(secret_names, sanitize_actions_name))
        return mapped
    mapped = _namespace_map(variable_names, sanitize_jenkins_env)
    mapped.update(_namespace_map(secret_names, sanitize_jenkins_id))
    return mapped


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
    mapped = reference_map(variable_names, secret_names, provider_is_actions=True)
    env = {name: f"${{{{ vars.{mapped[name]} }}}}" for name in variable_names}
    env.update({name: f"${{{{ secrets.{mapped[name]} }}}}" for name in secret_names})

    steps: list[dict] = [{"uses": "actions/checkout@v4"}]
    for command in _install_commands(repo_install):
        steps.append({"run": command})
    for path in script_paths:
        step: dict = {"name": f"QA: {path}", "run": f"python {path}"}
        if env:
            step["env"] = dict(env)
        steps.append(step)
    return steps


def jenkins_fallback_file(stage_src: str) -> str:
    """A stage fragment wrapped into a Jenkinsfile that stands on its own.

    Reached when ``insert_stage`` cannot resolve the repository's existing
    Jenkinsfile and the stage is written to a new file instead.  Wrapping
    costs four lines and makes the result a pipeline the team can actually
    run — where a bare fragment is a file that parses as nothing and clears
    none of the floor a created Jenkinsfile has to clear.
    """
    body = "\n".join(
        f"    {line}" if line.strip() else line for line in stage_src.strip("\n").splitlines()
    )
    return f"pipeline {{\n  agent any\n  stages {{\n{body}\n  }}\n}}\n"


def render_job_steps(steps: Sequence[dict]) -> str:
    """The Actions step list as YAML text, for the prompt to show the model.

    Rendered rather than described, because rule 3 of the system prompt
    tells the model to integrate these steps as given — it needs the actual
    bytes, not a summary of them.
    """
    stream = io.StringIO()
    round_trip_yaml().dump(list(steps), stream)
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

    **The binding runs the same direction as the Actions block**: the CI
    system supplies the mapped name and the script reads the sprint's own
    name, exactly as ``env: {ORIGINAL: ${{ vars.MAPPED }}}`` does. Written
    the other way round the script looks for a variable nothing sets, and —
    worse — degrades to a self-assignment whenever the two names coincide,
    which is precisely the case anyone checks by hand.

    ``withEnv`` rather than ``environment { }`` because a Groovy
    ``environment`` key must be a valid identifier: an env var named
    ``api.base-url`` is a syntax error there, while ``withEnv`` takes
    strings and carries any name the sprint defines.
    """
    mapped = reference_map(variable_names, secret_names, provider_is_actions=False)
    lines = ["stage('QA Agent E2E') {", "  steps {"]

    indent = "    "
    if variable_names:
        bindings = ", ".join(f'"{name}=${{env.{mapped[name]}}}"' for name in variable_names)
        lines.append(f"    withEnv([{bindings}]) {{")
        indent = "      "
    if secret_names:
        bindings = ", ".join(
            f"string(credentialsId: '{mapped[name]}', variable: '{name}')" for name in secret_names
        )
        lines.append(f"{indent}withCredentials([{bindings}]) {{")
        indent += "  "
    for command in _install_commands(repo_install):
        lines.append(f"{indent}sh '{command}'")
    for path in script_paths:
        lines.append(f"{indent}sh 'python {path}'")
    if secret_names:
        indent = indent[:-2]
        lines.append(f"{indent}}}")
    if variable_names:
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
        doc = safe_yaml().load(content)
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
    # Jenkins supplies these; only names the sprint was supposed to define
    # are ours to check. Actions needs no counterpart — `github.*` and
    # `runner.*` live outside the `vars`/`secrets` pattern entirely.
    return names - _JENKINS_BUILTINS


def validate(
    result,
    allowed_names: Sequence[str],
    originals: dict[str, str],
    *,
    provider_is_actions: bool,
    host_candidates: Sequence[str] | None = None,
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

    A **created file** and a **host edit** are checked differently on
    purpose: the first is a whole CI file, the second a job or stage
    fragment.  One floor applied to both would demand ``on``/``jobs`` of an
    Actions job body and ``pipeline {`` of a Groovy stage.

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
    created: list[tuple[str, str]] = []

    for item in result.files:
        if not _path_allowed(item.path):
            dropped.append(item.path)
            logger.warning("CI/CD export: refusing path outside the allowlist: %s", item.path)
            continue
        created.append((item.path, item.content))

    # A created file is a whole CI file and gets the whole-file floor.
    for path, content in created:
        if provider_is_actions and path.endswith((".yml", ".yaml")):
            _check_actions_structure(path, content)
        elif not provider_is_actions and "Jenkinsfile" in path:
            _check_jenkins_structure(path, content)
        _check_references(path, content, allowed, provider_is_actions)

    host_edit = getattr(result, "host_edit", None)
    if host_edit is not None:
        if host_edit.path not in originals:
            raise CicdValidationError(
                f"Host edit names {host_edit.path!r}, which this export did not fetch. "
                "Only a CI file read during this export may be edited."
            )
        # A job inherits its workflow's triggers, so adding one to an
        # `on: pull_request` host points an environment-dependent suite at
        # a live system on every pull request in a repository we do not
        # own. The prompt states the rule and names the legal hosts; a rule
        # of this consequence is gated rather than asked for, exactly as
        # the path allowlist and the fetched-file check are.
        if host_candidates is not None and host_edit.path not in host_candidates:
            raise CicdValidationError(
                f"Host edit names {host_edit.path!r}, whose triggers do not fit an "
                "environment-dependent suite. Add a job only to a workflow that already "
                "runs on dispatch, a schedule, or a completed deployment."
            )
        # A host edit is a **fragment**, not a file, so it gets the
        # fragment-level floor. Handing it the whole-file check would demand
        # `on`/`jobs` of an Actions job body and `pipeline {` of a Groovy
        # stage — rejecting every valid host edit on both sides.
        label = f"{host_edit.path} (added job)"
        _check_job_body(label, host_edit.job_body, provider_is_actions)
        _check_references(label, host_edit.job_body, allowed, provider_is_actions)

    return dropped


def _check_references(
    path: str, content: str, allowed: set[str], provider_is_actions: bool
) -> None:
    """Every variable/secret reference must resolve to a name we supplied.

    Otherwise the job runs with a silently blank value and the suite fails
    for reasons that have nothing to do with the product.
    """
    unknown = sorted(_referenced_names(content, provider_is_actions) - allowed)
    if unknown:
        raise CicdValidationError(
            f"{path} references {', '.join(unknown)}, which the sprint does not define. "
            "Every variable and secret must be one of: " + ", ".join(sorted(allowed))
        )


def _check_job_body(label: str, job_body: str, provider_is_actions: bool) -> None:
    """The fragment-level floor, provider-dispatched.

    Actions: a mapping carrying ``steps``, or the splice writes a string
    under ``jobs:`` and the workflow fails to parse in the target
    repository.  Jenkins: brace-balanced and declaring a stage, or
    ``insert_stage`` produces something that does not resolve.
    """
    if provider_is_actions:
        try:
            body = safe_yaml().load(job_body)
        except Exception as exc:
            raise CicdValidationError(f"The added job is not valid YAML: {exc}") from exc
        if not isinstance(body, dict) or "steps" not in body:
            raise CicdValidationError("The added job must be a mapping carrying 'steps'")
        return

    problems = jenkins_text.stage_check(job_body)
    if problems:
        raise CicdValidationError(f"{label} is not a usable stage: {'; '.join(problems)}")


def parse_job_body(job_body: str) -> dict:
    """The model's job fragment as a mapping, for ``add_job`` to splice in."""
    try:
        body = safe_yaml().load(job_body)
    except Exception as exc:
        raise WorkflowEditError(f"The added job is not valid YAML: {exc}") from exc
    if not isinstance(body, dict):
        raise WorkflowEditError("The added job must be a YAML mapping")
    return body


# ── The PR body ───────────────────────────────────────────────────────


def _case_inventory(cases: Sequence[TestCase]) -> list[str]:
    """Every committed case, grouped under the requirement it came from.

    A count told a reviewer how much arrived but nothing about what: the
    titles are what let them judge whether the suite covers the change in
    front of them, and the paths are how they find the script for a title
    that looks wrong.  Grouped by requirement because that is the unit the
    scripts are organised into on disk, so the list reads in the same shape
    as the diff.

    Insertion-ordered, following ``cases`` rather than sorting: the export
    already loaded them in a stable order, and re-sorting here would make
    the PR disagree with the commit for no gain.
    """
    grouped: dict[str, list[TestCase]] = {}
    for case in cases:
        grouped.setdefault(requirement_name(case) or "Unassigned", []).append(case)

    lines = [f"### Test cases in this pull request ({len(cases)})", ""]
    for name, members in grouped.items():
        lines.append(f"**{name}**")
        lines.append("")
        for case in members:
            lines.append(f"- {case.title} — `{script_path(case)}`")
        lines.append("")
    return lines


def pr_body(
    llm_prose: str,
    sprint_name: str,
    cases: Sequence[TestCase],
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
    "what must you do before this works".  It inventories the cases
    committed, names the variables and secrets the team has to create —
    **names only** — and any path the gate refused, so a dropped file is
    visible rather than silently missing.

    Everything below the rule is deterministic, and the prompt tells the
    model so (``CICD_SYSTEM_PROMPT`` rule 6): a reviewer reading the same
    setup instructions twice, once written by a model and once by us,
    cannot tell which one to trust when they differ.
    """
    parts = [llm_prose.strip(), "", "---", "", "## Generated by QA Agent", ""]
    parts.append(f"Sprint: **{sprint_name}**")
    parts.append("")
    parts.extend(_case_inventory(cases))

    if variable_names or secret_names:
        kind = "repository variables and secrets" if provider_is_actions else "credentials"
        parts.append(f"### Before this runs, create these {kind}")
        parts.append("")
        # Named through the one mapping every other consumer reads, and led
        # by the CI-side name: that is what the reader has to go and create.
        mapped = reference_map(
            variable_names, secret_names, provider_is_actions=provider_is_actions
        )
        for name in variable_names:
            suffix = f" (for the sprint's `{name}`)" if mapped[name] != name else ""
            parts.append(f"- variable `{mapped[name]}`{suffix}")
        for name in secret_names:
            suffix = f" (for the sprint's `{name}`)" if mapped[name] != name else ""
            parts.append(f"- secret `{mapped[name]}`{suffix}")
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
