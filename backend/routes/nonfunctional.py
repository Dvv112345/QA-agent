"""Nonfunctional-testing routes — run setup, runs, targets, load profiles, findings.

The setup call is **synchronous** inside the request (one cheap LLM call, no
tool loop), following the exploratory charter-generation precedent:
``LLMError`` maps to 502 and nothing is persisted.  Everything long-running
happens in the RQ task.

The create route is where this module differs from its exploratory twin, and
the difference is the whole safety story: a load profile describes traffic
this application will put on somebody else's environment, so *everything*
the setup call proposed is re-validated here as user input — the origin, the
method's permission, the placeholders in the body, and the ceilings — and the
clamped values are echoed back rather than silently applied.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from functools import partial

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from backend.config import (
    NONFUNCTIONAL_LOAD_STRESS_MIN_STEP_SECONDS,
    NONFUNCTIONAL_MAX_LOAD_PROFILES,
    server_limits,
)
from backend.database import get_session
from backend.models.database import (
    FindingSeverity,
    FindingType,
    LoadMethod,
    LoadShape,
    NonfunctionalDomain,
    NonfunctionalFinding,
    NonfunctionalLoadProfile,
    NonfunctionalRun,
    NonfunctionalRunStatus,
    NonfunctionalTarget,
    Requirement,
    Sprint,
    export_rollup,
    outdated_restart_error,
)
from backend.models.types import (
    DomainProposal,
    LoadProfileDraft,
    NonfunctionalLoadProfileResponse,
    NonfunctionalPlanDraftResponse,
    NonfunctionalPlanGenerateRequest,
    NonfunctionalRunCreateRequest,
    NonfunctionalRunDetailResponse,
    NonfunctionalRunResponse,
    NonfunctionalTargetResponse,
)
from backend.routes._common import (
    ensure_sprint_active,
    get_sprint_or_404,
    resolve_confirmed_env_vars,
    resolve_requirement_for_run,
    validate_url_vars,
)
from backend.services import finding_export, llm, load_runner, repo_reader
from backend.services.finding_export import TRACKER_REQUIRED_ERROR
from backend.services.llm_prompts import TestCaseLike
from backend.services.load_shapes import min_duration
from backend.services.queue import enqueue_rows, get_queue_service
from backend.utils import github_utils
from backend.utils.auth import verify_auth
from backend.utils.crypto import decrypt_token
from backend.utils.environment_utils import url_values
from backend.utils.nonfunctional_utils import (
    load_profile_summaries,
    parse_json_object,
    target_summaries,
)
from backend.utils.readme_utils import refresh_project_context, resolve_readme

logger = logging.getLogger(__name__)

# Completes "Sprint is finished — {}." for every gate in this module.
_GATE_SUBJECT = "nonfunctional runs can no longer be created"

router = APIRouter(dependencies=[Depends(verify_auth)])


# ── lookups ───────────────────────────────────────────────────────────


def _run_load_options():
    """Eager loads for a run response.

    ``outdated_reasons`` walks requirement → test_plan and sprint →
    test_environment, and both the list and detail endpoints poll every
    2.5 s — the same N+1 the exploratory list already avoids.
    """
    return (
        selectinload(NonfunctionalRun.requirement).selectinload(Requirement.test_plan),
        selectinload(NonfunctionalRun.targets).selectinload(NonfunctionalTarget.findings),
        selectinload(NonfunctionalRun.load_profiles),
        selectinload(NonfunctionalRun.sprint).selectinload(Sprint.test_environment),
    )


def _get_run_or_404(session: Session, run_id: int) -> NonfunctionalRun:
    run = session.exec(
        select(NonfunctionalRun).where(NonfunctionalRun.id == run_id).options(*_run_load_options())
    ).one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Nonfunctional run not found.")
    return run


# ── validation (everything the model proposed is user input by now) ───


def _validate_domains(domains: list[str]) -> list[str]:
    valid = {domain.value for domain in NonfunctionalDomain}
    if not domains:
        raise HTTPException(
            status_code=422,
            detail="Select at least one domain to examine — a run with none would check nothing.",
        )
    for domain in domains:
        if domain not in valid:
            raise HTTPException(status_code=422, detail=f"Unknown domain: '{domain}'.")
    # Deduplicated but order-preserving: the catalogue runs all of them at
    # every target, so a repeat would only double the work.
    return list(dict.fromkeys(domains))


def _stress_min_duration() -> int:
    return min_duration(LoadShape.STRESS, NONFUNCTIONAL_LOAD_STRESS_MIN_STEP_SECONDS)


def _validate_run_ceiling(max_users: int, max_total_requests: int) -> None:
    """The ceiling the user picked must sit inside the server's maximums.

    Checked at generate **and** at create: the first so the model sizes its
    proposals against a real number, the second because by then the
    ceiling has been through a form again.
    """
    limits = server_limits()
    for label, value, maximum in (
        ("Peak users", max_users, limits["max_users"]),
        ("Total requests", max_total_requests, limits["max_total_requests"]),
    ):
        if value < 1 or value > maximum:
            raise HTTPException(
                status_code=422,
                detail=f"{label} must be between 1 and {maximum}; got {value}.",
            )


def _validate_load_profiles(
    profiles: list[LoadProfileDraft],
    *,
    base_urls: list[str],
    env_vars: dict[str, str],
    environment_disposable: bool,
    max_users: int,
    max_total_requests: int,
) -> list[LoadProfileDraft]:
    """Re-check every profile and clamp it, returning what will actually run.

    Clamped rather than refused where a smaller number is still what the user
    meant: users to the run's peak (a stress profile always ramps to it), and
    duration to the shape's ceiling. The clamped values are what the response
    carries, so the user sees what they got.

    Everything else is a refusal, because each one means the profile would
    do something nobody approved — hit an origin outside the sprint's test
    environment, use a method the run is not permitted, send a body still
    carrying an unresolvable placeholder, run a stress ramp too short to have
    steps, or ask for more requests in total than the run's budget. The
    budget is refused rather than trimmed: it is the number the user
    consented to, and deciding which profile loses requests is theirs.

    This is the **only** place the run budget is enforced (D16): profiles
    cannot be edited after this, each cap is exact, and a launched profile
    is never re-sent, so Σ caps ≤ budget here holds for the whole run.
    """
    if len(profiles) > NONFUNCTIONAL_MAX_LOAD_PROFILES:
        raise HTTPException(
            status_code=422,
            detail=f"At most {NONFUNCTIONAL_MAX_LOAD_PROFILES} load profiles are allowed per run.",
        )

    allowed = load_runner.allowed_origins_for(base_urls)
    valid_shapes = {shape.value for shape in LoadShape}
    stress_min = _stress_min_duration()
    checked: list[LoadProfileDraft] = []
    for profile in profiles:
        method = (profile.method or "GET").upper()
        if method not in {member.value for member in LoadMethod}:
            raise HTTPException(status_code=422, detail=f"Unsupported HTTP method: '{method}'.")

        if profile.shape not in valid_shapes:
            raise HTTPException(
                status_code=422, detail=f"Unsupported load shape: '{profile.shape}'."
            )

        # The executor refuses these too. Refusing here as well is what lets
        # the user learn before a run exists rather than from a profile row
        # that recorded a refusal.
        refusal = load_runner.refusal_for(profile.url, allowed)
        if refusal is not None:
            raise HTTPException(status_code=422, detail=refusal)

        if not LoadMethod.is_safe(method) and not environment_disposable:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{method} changes data. Declare the environment disposable to allow "
                    "non-safe load methods, or use a safe method (GET, HEAD, OPTIONS)."
                ),
            )

        unknown = load_runner.unknown_placeholders(profile.body, env_vars)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Request body references environment variables that do not exist in "
                    f"this sprint: {', '.join(unknown)}."
                ),
            )

        # A stress duration the user typed is never stretched silently: five
        # steps shorter than the minimum would not be steps.
        if profile.shape == LoadShape.STRESS and profile.duration_seconds < stress_min:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A stress profile ramps in five steps and needs at least {stress_min} "
                    f"seconds; got {profile.duration_seconds}."
                ),
            )

        ceilings = load_runner.ceilings_for(method, environment_disposable=environment_disposable)
        users = (
            max_users
            if profile.shape == LoadShape.STRESS
            else max(1, min(profile.concurrency, max_users, ceilings.concurrency))
        )
        checked.append(
            LoadProfileDraft(
                url=profile.url,
                method=method,
                body=profile.body,
                shape=profile.shape,
                concurrency=users,
                duration_seconds=max(
                    1, min(profile.duration_seconds, load_runner.duration_ceiling(profile.shape))
                ),
                total_request_cap=max(1, min(profile.total_request_cap, ceilings.total_requests)),
                rationale=profile.rationale,
            )
        )

    requested = sum(profile.total_request_cap for profile in checked)
    if requested > max_total_requests:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Load profiles request {requested} in total; this run's budget is "
                f"{max_total_requests}."
            ),
        )
    return checked


def _compose_profiles(
    result: llm.NonfunctionalPlanResult,
    env_vars: dict[str, str],
    *,
    max_users: int,
    max_total_requests: int,
    environment_disposable: bool,
) -> list[LoadProfileDraft]:
    """Turn the model's (variable, path) pairs into absolute URLs.

    The model never sees a variable's value, so it cannot write a URL — it
    says *which key* and *which path*, and the host is resolved here.  That
    is what makes a proposed profile land on a confirmed origin **by
    construction** rather than by the model having guessed the host right.

    A profile naming a variable the model did not nominate is dropped, the
    same way an unknown domain is: it is malformed output, and composing a
    URL against an origin nobody nominated is worse than proposing nothing.

    The join is deliberately not ``urljoin``: ``urljoin`` treats a leading
    slash as root-relative and discards the base's own path, so a base of
    ``https://app.test/staging`` plus ``/api/x`` would silently become
    ``https://app.test/api/x`` — a different origin path, aimed at whatever
    lives there instead.  Base URLs with a path prefix are ordinary here.

    Then every proposal is fitted to the ceiling the user already picked.
    Each step is a prompt rule made deterministic, because the prompt is
    advice and this is the guarantee — the same pattern as the origin join::

        1. a variable nobody nominated          → dropped
        2. a non-safe method, no declaration    → dropped
        3. an unknown shape                     → constant load
        4. stress                               → users = the peak; duration raised
                                                  to the minimum (adds time, not
                                                  requests — the budget binds)
        5. any other shape                      → users clamped to the peak
        6. duration                             → clamped to the shape's ceiling
        7. request cap                          → taken from what the run budget
                                                  has left; nothing left → dropped
    """
    nominated = set(result.base_url_env_vars)
    valid_shapes = {shape.value for shape in LoadShape}
    stress_min = _stress_min_duration()
    remaining = max_total_requests
    composed: list[LoadProfileDraft] = []
    for profile in result.load_profiles:
        if profile.base_url_env_var not in nominated:
            logger.warning(
                "Dropping proposed load profile: '%s' was not nominated as a base URL.",
                profile.base_url_env_var,
            )
            continue
        method = (profile.method or "GET").upper()
        if not LoadMethod.is_safe(method) and not environment_disposable:
            logger.info(
                "Dropping proposed %s load profile: the environment is not declared disposable.",
                method,
            )
            continue
        shape = profile.shape if profile.shape in valid_shapes else LoadShape.LOAD.value
        if shape == LoadShape.STRESS:
            users = max_users
            duration = max(profile.duration_seconds, stress_min)
        else:
            users = max(1, min(profile.concurrency, max_users))
            duration = profile.duration_seconds
        duration = max(1, min(duration, load_runner.duration_ceiling(shape)))
        cap = min(max(1, profile.total_request_cap), remaining)
        if cap <= 0:
            logger.info("Dropping proposed load profile: the run's request budget is spent.")
            continue
        remaining -= cap

        base = env_vars[profile.base_url_env_var]
        path = (profile.path or "").strip()
        url = base.rstrip("/") + "/" + path.lstrip("/") if path.strip("/") else base
        composed.append(
            LoadProfileDraft(
                url=url,
                method=method,
                body=profile.body,
                shape=shape,
                concurrency=users,
                duration_seconds=duration,
                total_request_cap=cap,
                rationale=profile.rationale,
            )
        )
    return composed


# ── response builders (aggregates computed here, never stored) ────────


def _finding_counts(run: NonfunctionalRun) -> tuple[int, int, int]:
    bugs = issues = high = 0
    for target in run.targets:
        for finding in target.findings:
            if finding.finding_type == FindingType.BUG:
                bugs += 1
            elif finding.finding_type == FindingType.ISSUE:
                issues += 1
            if finding.severity == FindingSeverity.HIGH:
                high += 1
    return bugs, issues, high


def _run_fields(run: NonfunctionalRun) -> dict:
    bugs, issues, high = _finding_counts(run)
    return {
        "id": run.id,
        "sprint_id": run.sprint_id,
        "requirement_id": run.requirement_id,
        "requirement_name": run.requirement_name,
        "status": run.status,
        "domains": run.domains,
        "environment_disposable": run.environment_disposable,
        "max_users": run.max_users,
        "max_total_requests": run.max_total_requests,
        "summary": run.summary,
        "error": run.error,
        "outdated_reasons": run.outdated_reasons,
        "requirement_deleted": run.requirement_deleted,
        "target_count": len(run.targets),
        "load_profile_count": len(run.load_profiles),
        "bug_count": bugs,
        "issue_count": issues,
        "high_severity_count": high,
        **export_rollup(run.bug_findings, export_findings=run.export_findings),
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _run_response(run: NonfunctionalRun) -> NonfunctionalRunResponse:
    return NonfunctionalRunResponse(**_run_fields(run))


def _target_response(target: NonfunctionalTarget) -> NonfunctionalTargetResponse:
    return NonfunctionalTargetResponse(
        id=target.id,
        position=target.position,
        url=target.url,
        kind=target.kind,
        status=target.status,
        error=target.error,
        a11y_outcome=target.a11y_outcome,
        security_outcome=target.security_outcome,
        performance_outcome=target.performance_outcome,
        metrics=parse_json_object(target.metrics_json),
        finding_count=len(target.findings),
        updated_at=target.updated_at,
    )


def _profile_response(profile: NonfunctionalLoadProfile) -> NonfunctionalLoadProfileResponse:
    return NonfunctionalLoadProfileResponse(
        id=profile.id,
        position=profile.position,
        url=profile.url,
        method=profile.method,
        # Echoed with its placeholders unresolved, exactly as stored:
        # resolution happens inside the load runner precisely so no
        # resolved value is ever serialized.
        body=profile.body,
        shape=profile.shape,
        concurrency=profile.concurrency,
        duration_seconds=profile.duration_seconds,
        total_request_cap=profile.total_request_cap,
        status=profile.status,
        requests_sent=profile.requests_sent,
        launched_at=profile.launched_at,
        results=parse_json_object(profile.results_json),
        error=profile.error,
        updated_at=profile.updated_at,
    )


def _run_detail(run: NonfunctionalRun) -> NonfunctionalRunDetailResponse:
    findings = [finding for target in run.targets for finding in target.findings]
    urls = {target.id: target.url for target in run.targets}
    return NonfunctionalRunDetailResponse(
        **_run_fields(run),
        base_url_env_vars=run.base_url_env_vars,
        targets=[_target_response(target) for target in run.targets],
        load_profiles=[_profile_response(profile) for profile in run.load_profiles],
        findings=[
            {
                **finding.model_dump(),
                "url": urls.get(finding.nonfunctional_target_id, ""),
                "has_screenshot": finding.has_screenshot,
            }
            for finding in findings
        ],
    )


# ── run setup ─────────────────────────────────────────────────────────


@router.post(
    "/sprints/{sprint_id}/nonfunctional-plan/generate",
    response_model=NonfunctionalPlanDraftResponse,
)
async def generate_nonfunctional_plan(
    sprint_id: int,
    body: NonfunctionalPlanGenerateRequest,
    session: Session = Depends(get_session),
) -> NonfunctionalPlanDraftResponse:
    """Propose domains, base URLs and load profiles. Persists nothing."""
    sprint = get_sprint_or_404(session, sprint_id)
    ensure_sprint_active(sprint, _GATE_SUBJECT)
    requirement = resolve_requirement_for_run(sprint, body.requirement_id)
    env_vars = resolve_confirmed_env_vars(sprint)
    # Before the LLM call: a ceiling outside the server's maximums would size
    # every proposal against a number the create route will refuse.
    _validate_run_ceiling(body.max_users, body.max_total_requests)

    covered = [
        TestCaseLike(
            title=case.title,
            preconditions=case.preconditions,
            steps=case.steps,
            expected_result=case.expected_result,
            case_type=case.case_type,
            priority=case.priority,
        )
        for case in requirement.test_plan.cases
    ]
    readme = await resolve_readme(sprint)
    file_tree = sprint.repo.file_tree if sprint.repo else None

    # A load profile is only worth proposing if it names an endpoint that
    # exists, and the file tree lists paths without routes — so the model
    # reads source here. `build_read_file` calls `asyncio.run` internally,
    # which is safe ONLY because `generate_nonfunctional_plan` is reached
    # through `asyncio.to_thread` below: that runs it on a worker thread
    # with no event loop. Calling it inline from this async route would
    # raise. A sprint with no repo or no captured tree passes None, which
    # the loop degrades to a plain completion for.
    read_file = None
    if file_tree and sprint.repo:
        owner, repo_name = github_utils.parse_github_url(sprint.repo.github_link)
        read_file = repo_reader.build_read_file(
            file_tree,
            owner,
            repo_name,
            decrypt_token(sprint.repo.github_token) if sprint.repo.github_token else None,
        )

    # Names only, never values — split so the model can tell which key is a
    # candidate base URL without being shown one.
    urls = url_values(env_vars)
    url_names = sorted(name for name, value in env_vars.items() if value in urls)
    other_names = sorted(name for name in env_vars if name not in set(url_names))

    try:
        result = await asyncio.to_thread(
            llm.generate_nonfunctional_plan,
            name=requirement.name,
            description=requirement.description,
            covered_cases=covered,
            url_env_var_names=url_names,
            other_env_var_names=other_names,
            readme=readme,
            file_tree=file_tree,
            read_file=read_file,
            max_users=body.max_users,
            max_total_requests=body.max_total_requests,
            environment_disposable=body.environment_disposable,
        )
    except llm.LLMError as exc:
        logger.warning("Sprint id=%d: nonfunctional plan generation failed: %s", sprint_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # The model only ever saw variable *names*; confirm its nominations
    # resolve to real http(s) URLs before the user is asked to approve them.
    # Everything below relies on this having passed: it is what makes the
    # env_vars lookup in _compose_profiles safe.
    validate_url_vars(result.base_url_env_vars, env_vars, status_code=502)

    valid_domains = {domain.value for domain in NonfunctionalDomain}
    return NonfunctionalPlanDraftResponse(
        requirement_id=requirement.id,
        requirement_name=requirement.name,
        domains=[
            DomainProposal(
                domain=proposal.domain,
                applicable=proposal.applicable,
                rationale=proposal.rationale,
            )
            for proposal in result.domains
            if proposal.domain in valid_domains
        ],
        base_url_env_vars=result.base_url_env_vars,
        load_profiles=_compose_profiles(
            result,
            env_vars,
            max_users=body.max_users,
            max_total_requests=body.max_total_requests,
            environment_disposable=body.environment_disposable,
        ),
    )


# ── runs ──────────────────────────────────────────────────────────────


@router.post(
    "/sprints/{sprint_id}/nonfunctional-runs",
    response_model=NonfunctionalRunDetailResponse,
    status_code=201,
)
async def create_nonfunctional_run(
    sprint_id: int,
    body: NonfunctionalRunCreateRequest,
    session: Session = Depends(get_session),
) -> NonfunctionalRunDetailResponse:
    """Start a nonfunctional run for one requirement."""
    sprint = get_sprint_or_404(session, sprint_id)
    ensure_sprint_active(sprint, _GATE_SUBJECT)
    requirement = resolve_requirement_for_run(sprint, body.requirement_id)
    env_vars = resolve_confirmed_env_vars(sprint)

    # Everything the generate call returned has been through a form by now,
    # so none of it is trusted — all of it is re-validated from scratch.
    domains = _validate_domains(body.domains)
    validate_url_vars(body.base_url_env_vars, env_vars, status_code=422)
    base_urls = [env_vars[name] for name in body.base_url_env_vars]
    _validate_run_ceiling(body.max_users, body.max_total_requests)
    profiles = _validate_load_profiles(
        body.load_profiles,
        base_urls=base_urls,
        env_vars=env_vars,
        environment_disposable=body.environment_disposable,
        max_users=body.max_users,
        max_total_requests=body.max_total_requests,
    )

    if body.export_findings and sprint.issue_tracker is None:
        raise HTTPException(status_code=422, detail=TRACKER_REQUIRED_ERROR)

    if any(
        run.status in (NonfunctionalRunStatus.PENDING, NonfunctionalRunStatus.RUNNING)
        for run in requirement.nonfunctional_runs
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Requirement '{requirement.name}' already has a nonfunctional run in progress."
            ),
        )

    # Refresh project context once for the whole run.
    await refresh_project_context(session, sprint)

    run = NonfunctionalRun(
        sprint_id=sprint_id,
        requirement_id=requirement.id,
        # Recorded so a later edit upstream marks this run outdated.
        requirement_revision=requirement.content_revision,
        plan_revision=requirement.test_plan.content_revision,
        env_revision=sprint.test_environment.content_revision,
        base_url_env_vars_csv=",".join(body.base_url_env_vars),
        domains_csv=",".join(domains),
        environment_disposable=body.environment_disposable,
        max_users=body.max_users,
        max_total_requests=body.max_total_requests,
        export_findings=body.export_findings,
    )
    for position, profile in enumerate(profiles):
        NonfunctionalLoadProfile(
            nonfunctional_run=run,
            position=position,
            url=profile.url,
            method=profile.method,
            body=profile.body,
            shape=profile.shape,
            concurrency=profile.concurrency,
            duration_seconds=profile.duration_seconds,
            total_request_cap=profile.total_request_cap,
        )

    session.add(run)
    session.commit()
    session.refresh(run)
    enqueue_rows(session, [run], get_queue_service().enqueue_nonfunctional_run)

    logger.info(
        "Sprint id=%d: nonfunctional run %d created (%s) with %d load profile(s)",
        sprint_id,
        run.id,
        ", ".join(domains),
        len(profiles),
    )
    return _run_detail(_get_run_or_404(session, run.id))


@router.get(
    "/sprints/{sprint_id}/nonfunctional-runs", response_model=list[NonfunctionalRunResponse]
)
async def list_nonfunctional_runs(
    sprint_id: int,
    session: Session = Depends(get_session),
) -> list[NonfunctionalRunResponse]:
    """List a sprint's nonfunctional runs, newest first."""
    get_sprint_or_404(session, sprint_id)
    runs = session.exec(
        select(NonfunctionalRun)
        .where(NonfunctionalRun.sprint_id == sprint_id)
        .order_by(NonfunctionalRun.created_at.desc(), NonfunctionalRun.id.desc())
        .options(*_run_load_options())
    ).all()
    return [_run_response(run) for run in runs]


@router.get("/nonfunctional-runs/{run_id}", response_model=NonfunctionalRunDetailResponse)
async def get_nonfunctional_run(
    run_id: int,
    session: Session = Depends(get_session),
) -> NonfunctionalRunDetailResponse:
    """Fetch one run — its targets, load profiles, findings and roll-up."""
    return _run_detail(_get_run_or_404(session, run_id))


@router.get("/nonfunctional-findings/{finding_id}/screenshot")
async def get_nonfunctional_finding_screenshot(
    finding_id: int,
    session: Session = Depends(get_session),
) -> FileResponse:
    """Serve a finding's screenshot.

    404s when the finding carries none — the normal case when
    ``STORE_OFFLINE`` is disabled, which the UI renders as a finding without
    an image rather than a broken one.
    """
    finding = session.get(NonfunctionalFinding, finding_id)
    if finding is None or not finding.screenshot_path:
        raise HTTPException(status_code=404, detail="No screenshot available for this finding.")
    if not os.path.isfile(finding.screenshot_path):
        raise HTTPException(status_code=404, detail="Screenshot file is no longer available.")
    return FileResponse(finding.screenshot_path, media_type="image/png")


@router.post("/nonfunctional-runs/{run_id}/restart", response_model=NonfunctionalRunDetailResponse)
async def restart_nonfunctional_run(
    run_id: int,
    session: Session = Depends(get_session),
) -> NonfunctionalRunDetailResponse:
    """Restart a failed run (uncapped, user-initiated).

    This route never touches the child rows, and the two kinds resume
    differently — deliberately, because re-doing them costs different
    things:

    * **Load profiles** are skipped when ``requests_sent > 0``.  The check
      is that column and never ``status``: a restart could legitimately
      reset a status, and re-issuing requests against somebody's
      environment — duplicated *writes*, for a non-safe method — is not
      something a retry may decide on its own.
    * **Targets** are re-examined from scratch.  Re-reading a page costs a
      page load, so the task keeps no per-target resume state and a
      restarted run writes a *second* row per URL.  That is expected;
      findings still de-duplicate on ``(domain, rule, url)``.
    """
    run = _get_run_or_404(session, run_id)
    ensure_sprint_active(run.sprint, _GATE_SUBJECT)

    if run.status != NonfunctionalRunStatus.FAILED:
        raise HTTPException(
            status_code=422, detail="Only failed nonfunctional runs can be restarted."
        )
    if run.outdated_reasons:
        raise HTTPException(
            status_code=422,
            detail=outdated_restart_error(run.outdated_reasons, run.requirement_deleted),
        )

    run.status = NonfunctionalRunStatus.PENDING
    run.error = None
    run.retry_count = 0
    run.updated_at = datetime.now(timezone.utc)
    session.add(run)
    session.commit()
    # No refresh here: `enqueue_rows` commits and refreshes the row itself,
    # and the reload below re-reads it with its relationships eager-loaded.
    enqueue_rows(session, [run], get_queue_service().enqueue_nonfunctional_run)
    return _run_detail(_get_run_or_404(session, run_id))


@router.post(
    "/nonfunctional-runs/{run_id}/summarize", response_model=NonfunctionalRunDetailResponse
)
async def summarize_nonfunctional_run(
    run_id: int,
    session: Session = Depends(get_session),
) -> NonfunctionalRunDetailResponse:
    """Retry the best-effort summary the task may have left null.

    Synchronous, like plan generation — one cheap completion, no queue.
    """
    run = _get_run_or_404(session, run_id)
    if run.status != NonfunctionalRunStatus.COMPLETED:
        raise HTTPException(
            status_code=422,
            detail="Only completed nonfunctional runs can be summarized.",
        )

    requirement = run.requirement
    if requirement is None:
        raise HTTPException(status_code=422, detail="This run's requirement no longer exists.")

    try:
        result = await asyncio.to_thread(
            llm.summarize_nonfunctional,
            name=requirement.name,
            description=requirement.description,
            targets=target_summaries(run),
            load_profiles=load_profile_summaries(run),
            max_users=run.max_users,
            max_total_requests=run.max_total_requests,
        )
    except llm.LLMError as exc:
        logger.warning("Nonfunctional run %d: summary retry failed: %s", run_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    run.summary = result.summary
    run.updated_at = datetime.now(timezone.utc)
    session.add(run)
    session.commit()
    session.refresh(run)
    return _run_detail(_get_run_or_404(session, run_id))


@router.post(
    "/nonfunctional-runs/{run_id}/export-findings",
    response_model=NonfunctionalRunDetailResponse,
)
async def export_nonfunctional_run_findings(
    run_id: int,
    session: Session = Depends(get_session),
) -> NonfunctionalRunDetailResponse:
    """File this run's unfiled bug findings, on request.

    The third twin of ``POST /test-runs/{id}/export-findings`` — see it for
    why this is the manual half of the export rule rather than a fallback.
    """
    run = _get_run_or_404(session, run_id)
    if run.sprint is not None and run.sprint.issue_tracker is None:
        raise HTTPException(status_code=422, detail=TRACKER_REQUIRED_ERROR)

    # `requested=True` because the click is itself the consent the run's
    # toggle stands in for.
    await asyncio.to_thread(partial(finding_export.export_findings, session, run, requested=True))

    session.expire_all()
    return _run_detail(_get_run_or_404(session, run_id))
