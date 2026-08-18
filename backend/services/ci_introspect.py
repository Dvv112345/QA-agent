"""Read a repository's existing CI and answer questions about it.

Pure functions over YAML text — no LLM, no network, no database.  The point
is to hand the model *normalized facts with provenance* rather than raw
workflow files: a merged, repo-wide average matches nothing that actually
exists, and inheriting ``runs-on: [self-hosted, deploy]`` from a deploy job
sends the QA suite to a runner that never picks it up while reviewing as
correct.

Two rules shape everything here:

**Extraction never guesses.**  ``runs-on`` may be a string, a list, a
``${{ matrix.os }}`` expression or a ``{group, labels}`` object; a version
pin may be literal, matrix-derived or read from a file.  Several of those
legitimately resolve to *unknown*, and every unresolvable field is ``None``
or empty so the model falls back to our defaults instead of inheriting a
fiction.

**Nothing here raises.**  A malformed workflow in someone else's repository
is not an error condition for us — it is one fewer source of convention.
"""

import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

logger = logging.getLogger(__name__)

# YAML 1.1 parses a bare `on:` key as the boolean True. Every workflow
# reader has to look under both keys — see `_workflow_triggers`.
_TRIGGER_KEYS = ("on", True)

# Triggers that already satisfy the export's trigger policy: a job added to
# a host workflow inherits the host's triggers, and an environment-dependent
# suite must not fire on every push or pull request.
_ALLOWED_HOST_TRIGGERS = frozenset(
    {"workflow_dispatch", "schedule", "workflow_run", "deployment_status"}
)

_INSTALL_MARKERS = (
    "pip install",
    "pip3 install",
    "uv pip install",
    "uv sync",
    "poetry install",
    "pipenv install",
    "npm ci",
    "npm install",
    "pnpm install",
    "yarn install",
    "apt-get install",
    "apt install",
    "bundle install",
    "go mod download",
)

# Workflows that describe neither how this repo tests nor where it deploys.
# Excluded from the facts entirely rather than ranked last: a CodeQL job's
# runner and setup steps are a *misleading* convention source, not a weak one.
_NOISE_MARKERS = (
    "codeql",
    "dependabot",
    "dependency-review",
    "stale",
    "labeler",
    "release-drafter",
    "semantic-pull-request",
    "greetings",
    "lint",
    "pre-commit",
    "spellcheck",
    "delete_branch",
    "delete-branch",
)

_DEPLOY_MARKERS = ("deploy", "release", "publish", "ship", "rollout")
_E2E_MARKERS = ("e2e", "end-to-end", "end_to_end", "playwright", "cypress", "selenium", "browser")


def _safe_yaml() -> YAML:
    """A non-round-trip loader, for reading facts rather than editing."""
    parser = YAML(typ="safe")
    parser.allow_duplicate_keys = True
    return parser


def _round_trip_yaml() -> YAML:
    """A round-trip loader that preserves comments, quoting and key order."""
    parser = YAML()
    parser.preserve_quotes = True
    # Left at ruamel's defaults deliberately: re-indenting a host file would
    # churn every line of a diff that should show one added job.
    parser.width = 4096
    return parser


class WorkflowEditError(ValueError):
    """A host workflow cannot be extended, and saying so beats guessing.

    Raised only by ``add_job``, and only for the two cases where editing
    would silently do the wrong thing: a document that is not a workflow we
    can extend, and an edit whose own output no longer round-trips.
    """


@dataclass(frozen=True)
class WorkflowFacts:
    """What one workflow file says about how this repository runs CI.

    Every field is what the file *states*.  Nothing is inferred from a
    sibling workflow, and nothing is defaulted — an absent or unresolvable
    value stays ``None``/empty so ``render_facts`` can be silent about it
    rather than assert something the repository never said.
    """

    path: str
    name: str
    triggers: tuple[str, ...] = ()
    runs_on: str | None = None
    python_version: str | None = None
    node_version: str | None = None
    install_commands: tuple[str, ...] = ()
    installs_browsers: bool = False
    has_services: bool = False
    defaults_working_directory: str | None = None
    has_concurrency: bool = False
    env_keys: tuple[str, ...] = ()
    local_actions: tuple[str, ...] = ()
    purpose: str = "test"


@dataclass(frozen=True)
class ActionFacts:
    """What one composite action requires of a caller.

    Only ``required_inputs_without_default`` is load-bearing: wiring a
    ``uses:`` step to an action that demands an input we cannot supply
    produces a workflow that fails at parse time in the target repository,
    long after our PR was reviewed.
    """

    name: str
    is_composite: bool = False
    required_inputs_without_default: tuple[str, ...] = ()


# ── Reading ───────────────────────────────────────────────────────────


def _workflow_triggers(doc: dict) -> tuple[str, ...]:
    """Trigger names, read from **both** the `"on"` and boolean `True` keys.

    ``on:`` written bare is parsed by YAML 1.1 as the boolean ``True``, so a
    reader that looks only under ``"on"`` finds nothing and concludes the
    workflow has no triggers — which would make every such host look safe to
    add a job to.  Pinned by its own test so nobody "fixes" the lookup.
    """
    for key in _TRIGGER_KEYS:
        if key not in doc:
            continue
        value = doc[key]
        if isinstance(value, dict):
            return tuple(str(name) for name in value)
        if isinstance(value, list):
            return tuple(str(name) for name in value)
        if value is not None:
            return (str(value),)
    return ()


def _is_expression(value: Any) -> bool:
    """Whether a scalar is a GitHub expression, and so unresolvable here."""
    return isinstance(value, str) and "${{" in value


def _resolve_runs_on(value: Any) -> str | None:
    """``runs-on`` as a label string, or ``None`` when it cannot be resolved.

    Four shapes are legal and only two of them name a runner we could
    reuse.  A matrix expression resolves at run time and a
    ``{group, labels}`` object names a runner group that only exists in the
    target organization, so both answer ``None`` — the model falls back to
    our default rather than copying something meaningless.
    """
    if isinstance(value, str):
        return None if _is_expression(value) else value
    if isinstance(value, list):
        labels = [str(item) for item in value if not _is_expression(item)]
        if len(labels) != len(value):
            return None
        return ", ".join(labels) if labels else None
    return None


def _steps(job: dict) -> list[dict]:
    steps = job.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def _setup_version(steps: Sequence[dict], action: str, key: str) -> str | None:
    """A version pin from a ``setup-*`` action, when it is a literal.

    ``python-version-file`` and matrix expressions both answer ``None``:
    the pin exists but this file does not carry it.
    """
    for step in steps:
        uses = step.get("uses")
        if not isinstance(uses, str) or action not in uses:
            continue
        with_block = step.get("with")
        if not isinstance(with_block, dict):
            continue
        value = with_block.get(key)
        if value is None or _is_expression(value):
            continue
        return str(value)
    return None


def _job_blocks(doc: dict) -> list[dict]:
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []
    return [job for job in jobs.values() if isinstance(job, dict)]


def _working_directory(doc: dict, jobs: Sequence[dict]) -> str | None:
    """``defaults.run.working-directory``, workflow level first, then job.

    Read at both levels because this repository's own ``frontend_ci.yml``
    sets it on the job.  It matters either way: a QA job that inherits it
    runs ``qa-agent-tests/*.py`` from a subdirectory that does not contain
    them.
    """
    for scope in (doc, *jobs):
        defaults = scope.get("defaults")
        if not isinstance(defaults, dict):
            continue
        run = defaults.get("run")
        if isinstance(run, dict) and run.get("working-directory"):
            return str(run["working-directory"])
    return None


def parse_workflow(path: str, text: str) -> WorkflowFacts | None:
    """Extract one workflow's facts, or ``None`` when it will not parse.

    A malformed workflow is silently excluded from the facts rather than
    failing the export: the model then creates a new file instead of
    extending one, which is additive and visible in the PR.
    """
    try:
        doc = _safe_yaml().load(io.StringIO(text))
    except (YAMLError, ValueError) as exc:
        logger.info("Skipping unparseable workflow %s: %s", path, exc)
        return None
    if not isinstance(doc, dict):
        return None

    jobs = _job_blocks(doc)
    steps = [step for job in jobs for step in _steps(job)]
    runs = [str(step["run"]) for step in steps if isinstance(step.get("run"), str)]

    env_keys: list[str] = []
    for scope in (doc, *jobs):
        env = scope.get("env")
        if isinstance(env, dict):
            env_keys.extend(str(key) for key in env)

    runs_on = None
    for job in jobs:
        runs_on = _resolve_runs_on(job.get("runs-on"))
        if runs_on is not None:
            break

    facts = WorkflowFacts(
        path=path,
        name=str(doc.get("name") or path.rsplit("/", 1)[-1]),
        triggers=_workflow_triggers(doc),
        runs_on=runs_on,
        python_version=_setup_version(steps, "actions/setup-python", "python-version"),
        node_version=_setup_version(steps, "actions/setup-node", "node-version"),
        install_commands=tuple(
            command.strip()
            for command in runs
            if any(marker in command for marker in _INSTALL_MARKERS)
        ),
        installs_browsers=any("playwright install" in command for command in runs),
        has_services=any(isinstance(job.get("services"), dict) for job in jobs),
        defaults_working_directory=_working_directory(doc, jobs),
        has_concurrency="concurrency" in doc or any("concurrency" in job for job in jobs),
        env_keys=tuple(dict.fromkeys(env_keys)),
        local_actions=tuple(
            dict.fromkeys(
                str(step["uses"])
                for step in steps
                if isinstance(step.get("uses"), str) and str(step["uses"]).startswith("./")
            )
        ),
    )
    return replace(facts, purpose=classify_purpose(facts))


def is_reusable_workflow(doc: dict) -> bool:
    """Whether this document is invoked with ``on: workflow_call``.

    Structurally unusable as a host: a reusable workflow is called at *job*
    level, so our steps can never share a job with it.  Worth detecting
    explicitly rather than letting the model try.
    """
    return "workflow_call" in _workflow_triggers(doc)


def parse_composite_action(text: str) -> ActionFacts | None:
    """Extract a composite action's contract, or ``None`` when unparseable."""
    try:
        doc = _safe_yaml().load(io.StringIO(text))
    except (YAMLError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None

    runs = doc.get("runs")
    inputs = doc.get("inputs")
    required = []
    if isinstance(inputs, dict):
        for key, spec in inputs.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("required") and "default" not in spec:
                required.append(str(key))

    return ActionFacts(
        name=str(doc.get("name") or ""),
        is_composite=isinstance(runs, dict) and runs.get("using") == "composite",
        required_inputs_without_default=tuple(required),
    )


# ── Judging ───────────────────────────────────────────────────────────


def classify_purpose(facts: WorkflowFacts) -> str:
    """Which kind of convention source this workflow is.

    Precedence is stated rather than left to whichever branch happens to
    match first, because workflows routinely read as two things at once (a
    deploy pipeline that also runs tests):

    1. ``noise``  — nothing about how *this product* is tested or shipped.
       First because it is an exclusion: a CodeQL runner is a misleading
       source, not a weak one.
    2. ``e2e``    — the best analogue we have. Demonstrated by installing
       browsers, which is evidence rather than naming.
    3. ``deploy`` — a *bad* runner source and the *best* source for where
       the test environment comes from. Ahead of ``test`` because deploy
       pipelines commonly run tests too, and reading one as a test workflow
       is exactly how a QA job ends up queued to a deploy runner.
    4. ``test``   — the default, and the source for runner and setup idiom.
    """
    haystack = f"{facts.path} {facts.name}".lower()
    if any(marker in haystack for marker in _NOISE_MARKERS):
        return "noise"
    if facts.installs_browsers or any(marker in haystack for marker in _E2E_MARKERS):
        return "e2e"
    if any(marker in haystack for marker in _DEPLOY_MARKERS):
        return "deploy"
    return "test"


def triggers_allow_job(facts: WorkflowFacts) -> bool:
    """Whether a QA job may be added to this workflow rather than a new file.

    Triggers are workflow-level, not job-level, so a job added here inherits
    every trigger the host has.  An environment-dependent suite must not
    fire on a pull request, and adding ``schedule`` to *their* workflow to
    fix that would start firing *their* jobs nightly.  So: edit only a host
    that already fits, and create a new file otherwise.
    """
    if not facts.triggers:
        return False
    return all(trigger in _ALLOWED_HOST_TRIGGERS for trigger in facts.triggers)


def host_hazards(facts: WorkflowFacts) -> list[str]:
    """Workflow-level settings a new job would silently inherit.

    Read and neutralized, never assumed absent.  This repository's own
    ``frontend_ci.yml`` is the live example of the first one.
    """
    hazards = []
    if facts.defaults_working_directory:
        hazards.append(
            f"defaults.run.working-directory is '{facts.defaults_working_directory}' — "
            "an added job runs from there unless it overrides it"
        )
    if facts.env_keys:
        hazards.append(
            "workflow-level env is set (" + ", ".join(facts.env_keys) + ") — "
            "an added job inherits these values"
        )
    if facts.has_concurrency:
        hazards.append("a concurrency group is set — a later push can cancel this run mid-suite")
    return hazards


# ── Rendering ─────────────────────────────────────────────────────────


def _fact_lines(facts: WorkflowFacts) -> list[str]:
    lines = [f"- purpose: {facts.purpose}"]
    if facts.triggers:
        lines.append(f"- triggers: {', '.join(facts.triggers)}")
    if facts.runs_on:
        lines.append(f"- runs-on: {facts.runs_on}")
    if facts.python_version:
        lines.append(f"- python-version: {facts.python_version}")
    if facts.node_version:
        lines.append(f"- node-version: {facts.node_version}")
    for command in facts.install_commands:
        lines.append(f"- install step: {command}")
    if facts.installs_browsers:
        lines.append("- installs Playwright browsers: yes")
    if facts.has_services:
        lines.append("- declares service containers: yes")
    for hazard in host_hazards(facts):
        lines.append(f"- hazard: {hazard}")
    for action in facts.local_actions:
        lines.append(f"- uses local action: {action}")
    lines.append(
        "- a QA job may be added to this workflow: "
        + ("yes" if triggers_allow_job(facts) else "no — its triggers do not fit")
    )
    return lines


def render_facts(facts: Sequence[WorkflowFacts]) -> str:
    """The normalized table handed to the model — **per workflow, with provenance**.

    Never merged into one repo-wide blob.  A merged average matches no file
    that exists, and the model needs to know *which* workflow a convention
    came from to decide whether it is the right one to copy.
    """
    if not facts:
        return "No existing CI workflows were found in this repository."
    blocks = []
    for item in facts:
        blocks.append("\n".join([f"{item.path} ({item.name})", *_fact_lines(item)]))
    return "\n\n".join(blocks)


# ── Editing ───────────────────────────────────────────────────────────


def _unique_job_name(existing: Sequence[str], job_name: str) -> str:
    """A job id that does not collide with one the team already wrote.

    Overwriting is the failure this prevents: ``doc["jobs"][name] = body``
    on an existing key silently replaces a job, which reads in the diff as
    a plausible edit to it.
    """
    if job_name not in existing:
        return job_name
    candidate = f"{job_name}-qa-agent"
    suffix = 2
    while candidate in existing:
        candidate = f"{job_name}-qa-agent-{suffix}"
        suffix += 1
    return candidate


def add_job(text: str, job_name: str, job_body: dict) -> str:
    """Add one job to an existing workflow, leaving every other byte alone.

    ``jobs:`` is a YAML **mapping**, so insertion is a keyed write rather
    than a line offset — which is why this needs no location argument.

    Refuses rather than guesses in two cases: a document carrying no
    ``jobs`` key is not a workflow we can extend, and output that no longer
    matches the input outside the added job means the round trip perturbed
    something we did not intend to touch.
    """
    parser = _round_trip_yaml()
    try:
        doc = parser.load(io.StringIO(text))
    except (YAMLError, ValueError) as exc:
        raise WorkflowEditError(f"host workflow does not parse: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        raise WorkflowEditError("host workflow has no 'jobs' mapping to extend")

    name = _unique_job_name(list(doc["jobs"].keys()), job_name)
    doc["jobs"][name] = job_body

    stream = io.StringIO()
    parser.dump(doc, stream)
    output = stream.getvalue()

    _assert_only_added(text, output, name)
    return output


def _assert_only_added(before_text: str, after_text: str, added: str) -> None:
    """Cheap guard that the round trip changed nothing but the added job.

    Compares the two documents semantically with the new job removed.  It
    cannot see comment loss — ruamel preserves those by construction — but
    it does catch a re-serialization that dropped, reordered or retyped
    something elsewhere in the host file.
    """
    loader = _safe_yaml()
    try:
        before = loader.load(io.StringIO(before_text))
        after = loader.load(io.StringIO(after_text))
    except (YAMLError, ValueError) as exc:  # pragma: no cover - output we just wrote
        raise WorkflowEditError(f"edited workflow no longer parses: {exc}") from exc
    if isinstance(after, dict) and isinstance(after.get("jobs"), dict):
        after["jobs"].pop(added, None)
    if before != after:
        raise WorkflowEditError("editing the host workflow would change unrelated content")
