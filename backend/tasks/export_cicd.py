"""CI/CD export task, executed by the RQ worker.

One job per ``CicdExport``: reads the repository's own CI, asks the LLM to
author the configuration that runs an already-verified suite, validates it,
and writes a branch, a commit and a pull request.  Job args are the export
id only — everything else is read fresh from the database, which makes every
enqueue idempotent and reconciler-safe.

::

   export_cicd_task(cicd_export_id)
     │
     ├─ guard: row exists, status ∈ (PENDING, RUNNING)          ── else return
     ├─ guard: config present, sprint present                   ── else fail_row
     │         (sprint.active deliberately NOT checked)
     ├─ RUNNING + heartbeat; on_round = the heartbeat tick
     │
     ├─ asyncio.run ─── fetch_repo_metadata → default_branch
     │                  refresh the file tree
     ├─ re-derive eligibility from the DB                       ── empty → fail_row
     │    (a case archived since selection is skipped, not fatal)
     ├─ fetch CI files → originals{path: text}  (cap CICD_MAX_WORKFLOWS)
     │    Actions: parse_workflow → facts, drop `noise`
     │    Jenkins: raw text, one file, no parse
     ├─ build: deterministic block · var/secret name split · script files
     │
     ├─ generate_cicd_integration(…, read_file)          ← the ONE LLM call
     │    bounded tool loop; no file tree → plain completion
     │
     ├─ validate(result, …)                              ← raises = spend a retry
     │    path allowlist · host-edit target · structural floor
     │    · reference resolution
     │
     ├─ splice:  Actions → add_job(originals[p], name, body)
     │           Jenkins → insert_stage(originals[p], body)
     │                     └─ None → create Jenkinsfile.qa-agent
     │
     ├─ asyncio.run ─── ONE client, one sequence, 4 writes for any N:
     │                  tree(base=default SHA, inline content) → commit → ref → PR
     │
     ├─ CicdExportItem rows · COMPLETED · receipts · retry_count = 0
     │
     └─ except Exception → record_failure(…)   ── NEVER re-raise

**The sprint's ``active`` flag is deliberately never checked.**  A finished
sprint is exactly when a team wants its verified scripts committed, which is
also why ``CICD_EXPORT_SPEC``'s sweep counterpart carries
``inactive_sprint_ok=True``.

Playwright is not involved anywhere here, so the sync-API-vs-``asyncio.run``
ordering that shapes ``explore_requirement`` does not apply.

Must not import from ``backend.services.queue`` or ``backend.worker``
(circular-import rule).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets as secrets_module
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from backend.config import (
    CICD_MAX_WORKFLOWS,
    TEST_EXECUTION_FILE_MAX_CHARS,
)
from backend.database import new_session
from backend.models.database import (
    CicdExport,
    CicdExportItem,
    CicdExportStatus,
    CicdProvider,
)
from backend.services import cicd_eligibility, cicd_export, finalization, llm
from backend.services.ci_introspect import (
    add_job,
    parse_workflow,
    render_facts,
    triggers_allow_job,
)
from backend.services.jenkins_text import insert_stage
from backend.utils import github_utils
from backend.utils.crypto import decrypt_token
from backend.utils.environment_utils import variable_and_secret_names
from backend.utils.http_utils import SSL_CONTEXT
from backend.utils.readme_utils import resolve_readme

logger = logging.getLogger(__name__)

_FILE_TRUNCATION_MARKER = "\n… (truncated)"

_NO_CONFIG_ERROR = "No CI/CD target is connected for this sprint."
_NO_CASES_ERROR = "None of the selected test cases is still eligible to export."
_NO_REPO_ERROR = "This sprint has no registered repository to export to."

# Where a Jenkins stage lands when the existing Jenkinsfile cannot be
# spliced — a new file is additive and reviewable, a mangled one is not.
_JENKINS_FALLBACK_PATH = "Jenkinsfile.qa-agent"

_TOOLS_FALLBACK_NOTICE = (
    "The model could not read this repository during generation (the LLM provider "
    "rejected tool calls), so the CI below follows general conventions rather than "
    "this repository's own. Review the runner, setup steps and version pins."
)


def _branch_name(sprint_id: int) -> str:
    """A fresh branch per attempt — which is what makes a retry idempotent.

    The random suffix is not decoration: two workers finishing a restart
    race in the same second would otherwise pick the same name, and
    ``create_ref`` answers 422 for the loser.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"qa-agent/sprint-{sprint_id}-{stamp}-{secrets_module.token_hex(2)}"


def _build_read_file(
    file_tree: str, owner: str, repo: str, token: str | None
) -> Callable[[str], str]:
    """Executor for the LLM's read_file tool — same contract as ``execute_test``.

    Path-validated against the file tree, truncating, and never raising:
    errors go back to the model as strings it can react to.
    """
    allowed_paths = set(file_tree.splitlines())

    def read_file(path: str) -> str:
        requested = (path or "").strip().lstrip("/")
        if requested not in allowed_paths:
            return f"ERROR: could not read '{requested}': path is not in the repository file tree."
        try:
            content = asyncio.run(github_utils.fetch_file(owner, repo, requested, token))
        except github_utils.GitHubError as exc:
            return f"ERROR: could not read '{requested}': {exc}"
        if content is None:
            return f"ERROR: could not read '{requested}': file not found."
        if len(content) > TEST_EXECUTION_FILE_MAX_CHARS:
            content = content[:TEST_EXECUTION_FILE_MAX_CHARS] + _FILE_TRUNCATION_MARKER
        return content

    return read_file


def _ci_paths(file_tree: str | None, provider: str) -> list[str]:
    """Which repository paths this export should read as existing CI.

    Capped at ``CICD_MAX_WORKFLOWS``, which bounds both the request count
    and the prompt size.
    """
    if not file_tree:
        return []
    paths = []
    for line in file_tree.splitlines():
        path = line.strip()
        if provider == CicdProvider.GITHUB_ACTIONS:
            if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
                paths.append(path)
        elif path == "Jenkinsfile" or path.startswith("Jenkinsfile."):
            paths.append(path)
    return paths[:CICD_MAX_WORKFLOWS]


async def _fetch_ci_files(
    owner: str, repo: str, paths: list[str], token: str | None
) -> dict[str, str]:
    """The existing CI files, by path.

    A file that cannot be read is skipped rather than fatal: it is one
    fewer source of convention, and the model creates a new file instead of
    extending one — additive, and visible in the pull request.
    """
    originals: dict[str, str] = {}
    for path in paths:
        try:
            content = await github_utils.fetch_file(owner, repo, path, token)
        except github_utils.GitHubError as exc:
            logger.info("CI/CD export: skipping unreadable %s: %s", path, exc)
            continue
        if content:
            originals[path] = content
    return originals


async def _write_branch_and_pr(
    owner: str,
    repo: str,
    token: str,
    *,
    default_branch: str,
    branch: str,
    files: dict[str, str],
    commit_message: str,
    pr_title: str,
    pr_body: str,
) -> dict:
    """The whole write sequence, over **one** client.

    Five sequential requests against the same host, so one ``async with``
    owns the client and the helpers beneath receive it — the split this
    module's transport layer already implies.  ``SSL_CONTEXT`` is mandatory
    on Windows and belongs at this single construction site.

    Four of the five are writes, and that count is independent of how many
    test cases are being shipped: file content rides inline in the tree
    request rather than as a blob apiece.
    """
    async with httpx.AsyncClient(verify=SSL_CONTEXT) as client:
        base_sha = await github_utils.get_branch_sha(client, owner, repo, default_branch, token)
        tree_sha = await github_utils.create_tree(client, owner, repo, base_sha, files, token)
        commit_sha = await github_utils.create_commit(
            client, owner, repo, commit_message, tree_sha, base_sha, token
        )
        await github_utils.create_ref(client, owner, repo, branch, commit_sha, token)
        pull = await github_utils.create_pull_request(
            client, owner, repo, pr_title, pr_body, branch, default_branch, token
        )
    return {"commit_sha": commit_sha, **pull}


def _splice(result, originals: dict[str, str], provider: str, notices: list[str]) -> dict[str, str]:
    """Apply the model's host edit, returning ``{path: new content}``.

    The splice is **ours** on both sides: the model authors a job or stage
    body and never restates a whole file, so a truncating rewrite of the
    team's CI is not expressible.
    """
    if result.host_edit is None:
        return {}
    edit = result.host_edit
    original = originals[edit.path]  # validated to exist by cicd_export.validate

    if provider == CicdProvider.GITHUB_ACTIONS:
        body = cicd_export.parse_job_body(edit.job_body)
        return {edit.path: add_job(original, edit.job_name, body)}

    spliced = insert_stage(original, edit.job_body)
    if spliced is None:
        # A scanner that cannot resolve the file degrades to a new one
        # rather than corrupting the existing pipeline.
        notices.append(
            f"`{edit.path}` could not be edited safely, so the stage was written to "
            f"`{_JENKINS_FALLBACK_PATH}` instead. Merge it into your pipeline by hand."
        )
        return {_JENKINS_FALLBACK_PATH: edit.job_body}
    return {edit.path: spliced}


def export_cicd_task(cicd_export_id: int) -> None:
    """Export a sprint's verified scripts to its repository's CI, as a PR."""
    with new_session() as session:
        export = session.get(CicdExport, cicd_export_id)
        if export is None:
            logger.info("CI/CD export %d no longer exists — skipping", cicd_export_id)
            return
        if export.status not in (CicdExportStatus.PENDING, CicdExportStatus.RUNNING):
            logger.info(
                "CI/CD export %d is '%s' — skipping stale job", cicd_export_id, export.status
            )
            return

        sprint = export.sprint
        # `sprint.active` is deliberately not checked — see the module docstring.
        if sprint is None or sprint.repo is None:
            finalization.fail_row(session, finalization.CICD_EXPORT_SPEC, export, _NO_REPO_ERROR)
            return
        config = sprint.cicd_config
        if config is None:
            finalization.fail_row(session, finalization.CICD_EXPORT_SPEC, export, _NO_CONFIG_ERROR)
            return

        export.status = CicdExportStatus.RUNNING
        export.last_heartbeat = finalization.now()
        export.updated_at = finalization.now()
        session.add(export)
        session.commit()

        def on_round() -> None:
            export.last_heartbeat = finalization.now()
            session.add(export)
            session.commit()

        try:
            token = decrypt_token(config.access_token)
            owner, repo_name = github_utils.parse_github_url(sprint.repo.github_link)

            metadata = asyncio.run(github_utils.fetch_repo_metadata(owner, repo_name, token))
            default_branch = metadata["default_branch"]
            file_tree = asyncio.run(
                github_utils.fetch_file_tree(owner, repo_name, default_branch, token)
            )
            if file_tree:
                sprint.repo.file_tree = file_tree
                session.add(sprint.repo)
                session.commit()

            # Re-derived from the database rather than trusted from the
            # selection: a case archived between preview and job start is
            # skipped, not fatal.
            selected = set(export.selected_case_ids)
            entries = cicd_eligibility.case_entries(session, sprint)
            eligible = cicd_eligibility.eligible_ids(entries)
            case_ids = selected & eligible if selected else eligible
            cases = _load_cases(session, sprint, case_ids)
            if not cases:
                finalization.fail_row(
                    session, finalization.CICD_EXPORT_SPEC, export, _NO_CASES_ERROR
                )
                return

            provider = export.provider
            is_actions = provider == CicdProvider.GITHUB_ACTIONS
            env_vars = sprint.test_environment.env_vars if sprint.test_environment else {}
            variable_names, secret_names = variable_and_secret_names(env_vars)
            # The names the CI system will actually carry. Built once and
            # threaded through the prompt, the block and the gate — deriving
            # them separately is what made the gate refuse our own output.
            mapped = cicd_export.reference_map(
                variable_names, secret_names, provider_is_actions=is_actions
            )
            reference_names = list(mapped.values())

            scripts = cicd_export.script_files(cases)
            script_paths = sorted(scripts)

            ci_paths = _ci_paths(file_tree, provider)
            originals = asyncio.run(_fetch_ci_files(owner, repo_name, ci_paths, token))

            if is_actions:
                facts = [
                    fact
                    for fact in (parse_workflow(path, text) for path, text in originals.items())
                    if fact is not None and fact.purpose != "noise"
                ]
                ci_facts = render_facts(facts)
                host_candidates = [fact.path for fact in facts if triggers_allow_job(fact)]
                repo_install = [command for fact in facts for command in fact.install_commands]
                block = cicd_export.render_job_steps(
                    cicd_export.qa_job_steps(
                        script_paths, variable_names, secret_names, repo_install
                    )
                )
            else:
                # One Jenkinsfile per repository, so there is nothing to
                # merge and no provenance problem to solve — raw text is
                # the honest input.
                ci_facts = (
                    "\n\n".join(f"{path}:\n{text}" for path, text in originals.items())
                    or "No existing Jenkinsfile was found in this repository."
                )
                host_candidates = list(originals)
                block = cicd_export.jenkins_stage_block(script_paths, variable_names, secret_names)

            read_file = None
            if file_tree:
                read_file = _build_read_file(file_tree, owner, repo_name, token)

            readme = asyncio.run(resolve_readme(sprint))
            result = llm.generate_cicd_integration(
                provider=provider,
                readme=readme,
                file_tree=file_tree,
                ci_facts=ci_facts,
                ci_environment_hint=config.ci_environment_hint,
                # The model is offered the names it may actually reference —
                # the CI-side ones the deterministic block above emits, not
                # the sprint's own.
                variable_names=[mapped[name] for name in variable_names],
                secret_names=[mapped[name] for name in secret_names],
                script_paths=script_paths,
                deterministic_block=block,
                host_candidates=host_candidates,
                read_file=read_file,
                on_round=on_round,
            )

            dropped = cicd_export.validate(
                result,
                reference_names,
                originals,
                provider_is_actions=is_actions,
                host_candidates=host_candidates,
            )

            notices: list[str] = []
            if read_file is None:
                notices.append(_TOOLS_FALLBACK_NOTICE)

            allowed_new = {item.path: item.content for item in result.files}
            for path in dropped:
                allowed_new.pop(path, None)

            files: dict[str, str] = {}
            files.update(allowed_new)
            files.update(_splice(result, originals, provider, notices))
            # Our verified scripts are applied **last**, so no model-authored
            # file can displace one. The path allowlist already refuses the
            # script root, making this belt-and-braces — but the invariant
            # "the LLM never edits a verified script" is worth holding at the
            # write layer too, not only in the schema and the allowlist.
            files.update(scripts)

            branch = _branch_name(sprint.id)
            body = cicd_export.pr_body(
                result.pr_body,
                sprint.name,
                len(cases),
                variable_names,
                secret_names,
                dropped,
                provider_is_actions=is_actions,
                notes=result.notes,
                extra_notices=notices,
            )
            written = asyncio.run(
                _write_branch_and_pr(
                    owner,
                    repo_name,
                    token,
                    default_branch=default_branch,
                    branch=branch,
                    files=files,
                    commit_message=f"Add QA Agent test suite for sprint '{sprint.name}'",
                    pr_title=result.pr_title,
                    pr_body=body,
                )
            )

            # Receipts, written only now: an export that failed part-way
            # wrote nothing to the repository and must claim nothing.
            ci_file_paths = sorted(set(files) - set(scripts))
            export.items = [
                CicdExportItem(
                    test_case_id=case.id,
                    case_title=case.title,
                    requirement_name=_requirement_name(case),
                    committed_path=cicd_export.script_path(case),
                )
                for case in cases
            ]
            export.branch_name = branch
            export.commit_sha = written["commit_sha"]
            export.pr_number = written.get("number")
            export.pr_url = written.get("html_url")
            export.pr_title = result.pr_title
            export.notes = result.notes
            export.ci_file_paths_json = json.dumps(ci_file_paths)
            export.dropped_paths_json = json.dumps(dropped)
            # The CI-side names, so the receipt records what the team was
            # asked to create rather than the sprint's own vocabulary.
            export.variable_names_json = json.dumps([mapped[n] for n in variable_names])
            export.secret_names_json = json.dumps([mapped[n] for n in secret_names])
            export.status = CicdExportStatus.COMPLETED
            export.last_heartbeat = None
            export.retry_count = 0
            export.error = None
            export.updated_at = finalization.now()
            session.add(export)
            session.commit()
            logger.info(
                "CI/CD export %d completed: %s (%d cases)",
                cicd_export_id,
                export.pr_url,
                len(cases),
            )
        except Exception as exc:
            # Never re-raise: the DB retry counter, not RQ's failed
            # registry, is the recovery mechanism.
            logger.exception("CI/CD export failed for export %d", cicd_export_id)
            finalization.record_failure(session, finalization.CICD_EXPORT_SPEC, cicd_export_id, exc)


def _requirement_name(case) -> str:
    """The requirement a case belongs to, copied onto the receipt."""
    plan = case.test_plan
    requirement = plan.requirement if plan is not None else None
    return requirement.name if requirement is not None else ""


def _load_cases(session, sprint, case_ids: set[int]) -> list:
    """The selected cases, in a stable order, with their plan chain loaded.

    Walked off the sprint rather than queried by id so archived rows are
    filtered by the same properties everything else reads through.
    """
    cases = []
    for requirement in sprint.requirements:
        plan = requirement.test_plan
        if plan is None:
            continue
        for case in plan.cases:
            if case.id in case_ids:
                cases.append(case)
    return cases
