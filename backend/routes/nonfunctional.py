"""Nonfunctional-testing routes — run setup, runs, targets, load profiles, findings.

The setup call is **synchronous** inside the request (one cheap LLM call, no
tool loop), following the exploratory charter-generation precedent:
``LLMError`` maps to 502 and nothing is persisted.  Everything long-running
happens in the RQ task.

The create route is where this module differs from its exploratory twin, and
the difference is the whole safety story: a load profile describes traffic
this application will put on somebody else's environment, so *everything*
the setup call proposed is re-validated here as user input — the origin, the
method's tier, the placeholders in the body, and the ceilings — and the
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
    NONFUNCTIONAL_LOAD_MAX_CONCURRENCY,
    NONFUNCTIONAL_LOAD_MAX_DURATION_SECONDS,
    NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS,
    NONFUNCTIONAL_LOAD_UNSAFE_MAX_CONCURRENCY,
    NONFUNCTIONAL_LOAD_UNSAFE_MAX_TOTAL_REQUESTS,
    NONFUNCTIONAL_MAX_LOAD_PROFILES,
)
from backend.database import get_session
from backend.models.database import (
    FindingSeverity,
    FindingType,
    LoadMethod,
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
from backend.services import finding_export, llm, load_runner
from backend.services.finding_export import TRACKER_REQUIRED_ERROR
from backend.services.llm_prompts import TestCaseLike
from backend.services.queue import enqueue_rows, get_queue_service
from backend.utils.auth import verify_auth
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


def _ceilings(environment_disposable: bool) -> dict:
    """The two tiers, as the response echoes them back (Convention #10)."""
    return {
        "max_concurrency": NONFUNCTIONAL_LOAD_MAX_CONCURRENCY,
        "max_duration_seconds": NONFUNCTIONAL_LOAD_MAX_DURATION_SECONDS,
        "max_total_requests": NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS,
        "unsafe_max_concurrency": NONFUNCTIONAL_LOAD_UNSAFE_MAX_CONCURRENCY,
        "unsafe_max_total_requests": NONFUNCTIONAL_LOAD_UNSAFE_MAX_TOTAL_REQUESTS,
        "safe_methods": sorted(LoadMethod.safe_methods()),
    }


def _validate_load_profiles(
    profiles: list[LoadProfileDraft],
    *,
    base_urls: list[str],
    env_vars: dict[str, str],
    environment_disposable: bool,
) -> list[LoadProfileDraft]:
    """Re-check every profile and clamp it, returning what will actually run.

    Clamped rather than refused: the numbers came through a form, and a run
    that quietly does *less* than asked is the safe direction. The clamped
    values are what the response carries, so the user sees what they got.

    Everything else is a refusal, because each one means the profile would
    do something nobody approved — hit an origin outside the sprint's test
    environment, use a method the run is not permitted, or send a body still
    carrying an unresolvable placeholder.
    """
    if len(profiles) > NONFUNCTIONAL_MAX_LOAD_PROFILES:
        raise HTTPException(
            status_code=422,
            detail=f"At most {NONFUNCTIONAL_MAX_LOAD_PROFILES} load profiles are allowed per run.",
        )

    allowed = load_runner.allowed_origins_for(base_urls)
    checked: list[LoadProfileDraft] = []
    for profile in profiles:
        method = (profile.method or "GET").upper()
        if method not in {member.value for member in LoadMethod}:
            raise HTTPException(status_code=422, detail=f"Unsupported HTTP method: '{method}'.")

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

        ceilings = load_runner.ceilings_for(method, environment_disposable=environment_disposable)
        checked.append(
            LoadProfileDraft(
                url=profile.url,
                method=method,
                body=profile.body,
                concurrency=max(1, min(profile.concurrency, ceilings.concurrency)),
                duration_seconds=max(1, min(profile.duration_seconds, ceilings.duration_seconds)),
                total_request_cap=max(1, min(profile.total_request_cap, ceilings.total_requests)),
                rationale=profile.rationale,
            )
        )
    return checked


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
        concurrency=profile.concurrency,
        duration_seconds=profile.duration_seconds,
        total_request_cap=profile.total_request_cap,
        status=profile.status,
        requests_sent=profile.requests_sent,
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

    try:
        result = await asyncio.to_thread(
            llm.generate_nonfunctional_plan,
            name=requirement.name,
            description=requirement.description,
            covered_cases=covered,
            env_var_names=list(env_vars.keys()),
            readme=readme,
            file_tree=file_tree,
        )
    except llm.LLMError as exc:
        logger.warning("Sprint id=%d: nonfunctional plan generation failed: %s", sprint_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # The model only ever saw variable *names*; confirm its nominations
    # resolve to real http(s) URLs before the user is asked to approve them.
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
        load_profiles=[
            LoadProfileDraft(
                url=profile.url,
                method=(profile.method or "GET").upper(),
                body=profile.body,
                concurrency=profile.concurrency,
                duration_seconds=profile.duration_seconds,
                total_request_cap=profile.total_request_cap,
                rationale=profile.rationale,
            )
            for profile in result.load_profiles
        ],
        **_ceilings(environment_disposable=False),
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
    profiles = _validate_load_profiles(
        body.load_profiles,
        base_urls=base_urls,
        env_vars=env_vars,
        environment_disposable=body.environment_disposable,
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
        export_findings=body.export_findings,
    )
    for position, profile in enumerate(profiles):
        NonfunctionalLoadProfile(
            nonfunctional_run=run,
            position=position,
            url=profile.url,
            method=profile.method,
            body=profile.body,
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
