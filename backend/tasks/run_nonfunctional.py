"""Nonfunctional-testing task, executed by the RQ worker.

One job per run (``NonfunctionalRun``), covering one requirement: walk the
feature in a live browser, run the full catalogue at every URL the walk
lands on, examine the endpoints the application called for itself, then
apply the approved load profiles.  Job args are the run id only — everything
else is read fresh from the database, which makes every enqueue idempotent
and reconciler-safe.

**The catalogue runs on arrival, not on request.**  The model's tool surface
here is navigation and nothing else; the checks fire from ``on_navigated``
the moment the URL changes.  A tool the model calls is a tool it can decline
to call, and "the full catalogue at every target" would quietly become
"wherever it remembered to look".

Two orderings are load-bearing and commented at their call sites:

* README/file-tree resolution runs ``asyncio.run`` and therefore must
  complete **before** any browser exists — Playwright's sync API refuses to
  operate while a loop runs in the same thread.
* Load profiles run **last**, after the browser is closed, because the
  cookies they carry are read from it and because traffic during the walk
  would perturb the very timings the walk is measuring.

Resumability differs between the two child types, and the difference is the
one genuinely dangerous rule in this feature: a target is re-examined freely
(re-reading a page costs a page load), while **a load profile that already
sent traffic is never re-sent**, whatever its status says.  That is read
from ``requests_sent``, never from ``status``.

Must not import from ``backend.services.queue`` or ``backend.worker``
(circular-import rule).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlmodel import Session, select

from backend.config import (
    EXPLORATORY_SNAPSHOT_WINDOW,
    NONFUNCTIONAL_CATALOGUE_TIMEOUT,
    NONFUNCTIONAL_LOAD_REQUEST_TIMEOUT,
    NONFUNCTIONAL_MAX_ACTIONS,
    NONFUNCTIONAL_MAX_TARGETS,
)
from backend.database import new_session
from backend.models.database import (
    REQUIREMENT_DELETED_ERROR,
    SPRINT_FINISHED_ERROR,
    SUPERSEDED_ERROR,
    DomainOutcome,
    LoadMethod,
    NonfunctionalChildStatus,
    NonfunctionalDomain,
    NonfunctionalFinding,
    NonfunctionalLoadProfile,
    NonfunctionalRun,
    NonfunctionalRunStatus,
    NonfunctionalTarget,
    TargetKind,
)
from backend.services import (
    browser_session,
    finalization,
    finding_export,
    finding_grouping,
    llm,
    load_runner,
    nonfunctional_checks,
)
from backend.services.llm_prompts import TestCaseLike, ViolationLike
from backend.services.storage import StorageService
from backend.utils.environment_utils import browser_environment, redactable_items
from backend.utils.nonfunctional_utils import load_profile_summaries, target_summaries
from backend.utils.readme_utils import resolve_readme

logger = logging.getLogger(__name__)

# Should be unreachable via normal flow — guarded per this codebase's
# convention of never trusting a supposedly-impossible state blindly.
_ENV_VARS_MISSING_ERROR = "Test environment access variables have not been established."
_NO_BASE_URL_ERROR = "No application URL is available for this run."

# Bytes of an endpoint response body kept for the stack-trace rule.
_ENDPOINT_BODY_SAMPLE_CHARS = 4000


def _heartbeat(session: Session, run: NonfunctionalRun) -> None:
    """Mark the run alive so live work is never swept as a crashed worker."""
    run.last_heartbeat = finalization.now()
    session.add(run)
    session.commit()


def _canonical_url(url: str) -> str:
    """One spelling per page, so the same target is not examined twice.

    ``seen_urls`` is a plain string set, so ``http://app`` and
    ``http://app/`` were two targets carrying two sets of findings for one
    page.  The browser answers with its own normalization and a nominated
    env var need not match it, so the two spellings genuinely do meet.

    Deliberately conservative.  Scheme and host are case-insensitive per RFC
    3986 and an empty path means ``/``; the query is kept, because two query
    strings are usually two pages, and the path keeps its case, because
    paths are case-sensitive.
    """
    split = urlsplit(url.split("#", 1)[0])
    return urlunsplit(
        (split.scheme.lower(), split.netloc.lower(), split.path or "/", split.query, "")
    )


@dataclass
class _Catalogue:
    """Runs the checks at one URL and writes the target row.

    A class rather than a closure because the state is real: the accumulator
    that de-duplicates violations across targets, the position counter, and
    the cap on how many URLs a run examines all outlive any single arrival.

    Every method here is called from inside a browser executor, so nothing
    may raise — ``BrowserSession`` catches it, but a raise there costs the
    whole session where a recorded ``failed_to_run`` costs one target.
    """

    session: Session
    run: NonfunctionalRun
    browser: browser_session.BrowserSession | None = None
    violations: nonfunctional_checks.RunViolations = field(
        default_factory=nonfunctional_checks.RunViolations
    )
    seen_urls: set[str] = field(default_factory=set)
    position: int = 0
    # Screenshot path per target id, so a finding created later at triage —
    # when the page is long gone — can still carry the picture.
    screenshots: dict[int, str | None] = field(default_factory=dict)

    @property
    def domains(self) -> set[str]:
        return set(self.run.domains)

    def on_navigated(self, url: str, deadline: float) -> str:
        """Examine a newly reached URL. Returns a note for the model.

        The note deliberately says nothing about what was found: the model
        is not judging this application, and telling it what the checks
        found would invite it to start.
        """
        clean = _canonical_url(url)
        if clean in self.seen_urls:
            return ""
        if len(self.seen_urls) >= NONFUNCTIONAL_MAX_TARGETS:
            # Stop making targets, but do not end the loop: the walk may
            # still need to pass through pages to reach something, and a
            # hard stop would strand it mid-flow.
            return f"[{NONFUNCTIONAL_MAX_TARGETS} URLs examined — the limit for this run]"
        self.seen_urls.add(clean)

        target = self._new_target(clean, TargetKind.PAGE)
        try:
            self._examine_page(target, deadline)
        except Exception as exc:  # never raises — see the class docstring
            logger.exception("Examining %s failed", clean)
            target.status = NonfunctionalChildStatus.ERROR
            target.error = str(exc)[: finalization.ERROR_SUMMARY_MAX_CHARS]
            self._save(target)
        return f"[examined {clean}]"

    # ── target rows ───────────────────────────────────────────────────

    def record_unreachable(self, url: str, error: str) -> None:
        """Record a base URL the browser could not open.

        Part of the coverage floor rather than an aside: the run promises
        to examine every confirmed base URL, so one it could not reach has
        to say so.  Silence here would read exactly like a URL that was
        never nominated.
        """
        clean = _canonical_url(url)
        if clean in self.seen_urls:
            return
        self.seen_urls.add(clean)
        target = self._new_target(clean, TargetKind.PAGE)
        target.status = NonfunctionalChildStatus.ERROR
        target.error = error[: finalization.ERROR_SUMMARY_MAX_CHARS]
        for domain, attribute in (
            (NonfunctionalDomain.ACCESSIBILITY, "a11y_outcome"),
            (NonfunctionalDomain.SECURITY, "security_outcome"),
            (NonfunctionalDomain.PERFORMANCE, "performance_outcome"),
        ):
            if domain in self.domains:
                setattr(target, attribute, DomainOutcome.FAILED_TO_RUN)
        self._save(target)

    def _new_target(self, url: str, kind: str) -> NonfunctionalTarget:
        target = NonfunctionalTarget(
            nonfunctional_run_id=self.run.id,
            position=self.position,
            url=url,
            kind=kind,
            status=NonfunctionalChildStatus.RUNNING,
        )
        self.position += 1
        self.session.add(target)
        self.session.commit()
        self.session.refresh(target)
        return target

    def _save(self, target: NonfunctionalTarget) -> None:
        target.updated_at = finalization.now()
        self.session.add(target)
        # The heartbeat rides along: a catalogue costs seconds inside a tool
        # call, and the run must not look like a dead worker while it runs.
        self.run.last_heartbeat = finalization.now()
        self.session.add(self.run)
        self.session.commit()

    @staticmethod
    def _outcome(violations: list, ran: bool) -> str:
        if not ran:
            return DomainOutcome.FAILED_TO_RUN
        return DomainOutcome.VIOLATIONS if violations else DomainOutcome.CLEAN

    # ── the catalogue itself ──────────────────────────────────────────

    def _examine_page(self, target: NonfunctionalTarget, deadline: float) -> None:
        """Run every selected domain against the page currently loaded.

        The deadline is honoured **between** domains rather than inside
        one: a synchronous callback on the browser's own thread cannot be
        preempted, so a budget that pretended otherwise would be a lie. A
        domain the budget cuts off records ``failed_to_run`` — never
        silence.
        """
        # Not an `assert`: that vanishes under `python -O`, leaving an
        # AttributeError several lines down, and `str(AssertionError())` is
        # the empty string — which is precisely how an earlier ordering bug
        # here wrote `status=error` with a blank message and hid itself in
        # the data. `_walk_the_feature` assigns the browser before
        # `__enter__`, so this is unreachable; it exists to be legible if
        # that ordering ever regresses.
        if self.browser is None:  # pragma: no cover - see the comment above
            raise RuntimeError("the catalogue has no browser — the target cannot be examined")
        state = f"target {target.position}"

        if NonfunctionalDomain.ACCESSIBILITY in self.domains:
            # Applicability first, and it is decided by what the server
            # served rather than by how this target was reached: the walk
            # can navigate to a raw API URL, and Chromium's JSON viewer
            # hands axe a real DOM for a body that has no user interface at
            # all. `not_applicable` before `failed_to_run`, because a
            # document axe was never going to examine did not run out of
            # budget — it was never in scope.
            if not nonfunctional_checks.accessibility_applies(self.browser.document_content_type()):
                target.a11y_outcome = DomainOutcome.NOT_APPLICABLE
            elif self._expired(deadline):
                target.a11y_outcome = DomainOutcome.FAILED_TO_RUN
                target.error = _budget_error("accessibility")
            else:
                target.a11y_outcome = self._run_accessibility(target, state)

        if NonfunctionalDomain.SECURITY in self.domains:
            if self._expired(deadline):
                target.security_outcome = DomainOutcome.FAILED_TO_RUN
                target.error = _budget_error("security")
            else:
                target.security_outcome = self._run_security(target, state)

        if NonfunctionalDomain.PERFORMANCE in self.domains:
            if self._expired(deadline):
                target.performance_outcome = DomainOutcome.FAILED_TO_RUN
                target.error = _budget_error("performance")
            else:
                target.performance_outcome = self._run_performance(target)

        # One screenshot for the whole target, taken while the page is still
        # on screen. Findings are created later, at triage, and copy it.
        self.screenshots[target.id] = None
        png = self.browser.screenshot()
        if png is not None:
            self.screenshots[target.id] = _store_screenshot(self.run, target, png, self.session)

        target.status = NonfunctionalChildStatus.COMPLETED
        self._save(target)

    @staticmethod
    def _expired(deadline: float) -> bool:
        return time.monotonic() >= deadline

    def _run_accessibility(self, target: NonfunctionalTarget, state: str) -> str:
        outcome = self.browser.scan_accessibility()
        if not outcome.ok:
            target.error = outcome.error
            return DomainOutcome.FAILED_TO_RUN
        try:
            scan = nonfunctional_checks.axe_violations(outcome.data, target.url)
        except nonfunctional_checks.CheckError as exc:
            # A payload that is not the shape axe promises is `failed_to_run`,
            # never `clean`: reading it as clean would report every page
            # clean forever, silently and permanently.
            target.error = str(exc)
            return DomainOutcome.FAILED_TO_RUN
        self.violations.extend(scan.violations, state=state)
        return self._outcome(scan.violations, ran=True)

    def _run_security(self, target: NonfunctionalTarget, state: str) -> str:
        outcome = self.browser.check_headers()
        if not outcome.ok:
            target.error = outcome.error
            return DomainOutcome.FAILED_TO_RUN
        data = outcome.data
        try:
            found = nonfunctional_checks.passive_security_violations(
                url=target.url,
                status=data.get("status"),
                headers=data.get("headers"),
                cookies=data.get("cookies"),
                body_sample=data.get("body_sample"),
            )
        except nonfunctional_checks.CheckError as exc:
            target.error = str(exc)
            return DomainOutcome.FAILED_TO_RUN
        self.violations.extend(found, state=state)
        return self._outcome(found, ran=True)

    def _run_performance(self, target: NonfunctionalTarget) -> str:
        outcome = self.browser.measure_performance()
        if not outcome.ok:
            target.error = outcome.error
            return DomainOutcome.FAILED_TO_RUN
        target.metrics_json = _dump(outcome.data)
        # Measured, stored, and never judged: performance produces no
        # finding, no defect and no ticket, so its outcome is only ever
        # "we measured it".
        return DomainOutcome.CLEAN

    # ── endpoints ─────────────────────────────────────────────────────

    def examine_endpoint(self, url: str, cookies: dict[str, str]) -> None:
        """Examine one XHR/fetch URL the application called for itself.

        Accessibility does not apply to a JSON response, and that is
        recorded as ``not_applicable`` rather than left blank — the run says
        what it did not look at, so a blank never has to be interpreted.
        """
        clean = _canonical_url(url)
        if clean in self.seen_urls or len(self.seen_urls) >= NONFUNCTIONAL_MAX_TARGETS:
            return
        self.seen_urls.add(clean)
        target = self._new_target(clean, TargetKind.ENDPOINT)

        if NonfunctionalDomain.ACCESSIBILITY in self.domains:
            target.a11y_outcome = DomainOutcome.NOT_APPLICABLE

        try:
            started = time.monotonic()
            with httpx.Client(
                verify=load_runner.SSL_CONTEXT,
                timeout=NONFUNCTIONAL_LOAD_REQUEST_TIMEOUT,
                follow_redirects=False,
                cookies=cookies,
            ) as client:
                response = client.get(clean)
            elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        except Exception as exc:
            logger.info("Endpoint %s could not be examined: %s", clean, exc)
            target.status = NonfunctionalChildStatus.ERROR
            target.error = str(exc)[: finalization.ERROR_SUMMARY_MAX_CHARS]
            if NonfunctionalDomain.SECURITY in self.domains:
                target.security_outcome = DomainOutcome.FAILED_TO_RUN
            if NonfunctionalDomain.PERFORMANCE in self.domains:
                target.performance_outcome = DomainOutcome.FAILED_TO_RUN
            self._save(target)
            return

        if NonfunctionalDomain.SECURITY in self.domains:
            try:
                found = nonfunctional_checks.passive_security_violations(
                    url=clean,
                    status=response.status_code,
                    headers=dict(response.headers),
                    cookies=None,  # cookies belong to the browser, not to this request
                    body_sample=response.text[:_ENDPOINT_BODY_SAMPLE_CHARS],
                )
                self.violations.extend(found, state="endpoint")
                target.security_outcome = self._outcome(found, ran=True)
            except nonfunctional_checks.CheckError as exc:
                target.error = str(exc)
                target.security_outcome = DomainOutcome.FAILED_TO_RUN

        if NonfunctionalDomain.PERFORMANCE in self.domains:
            # Authenticated, like the load profiles: an unauthenticated
            # reference would be the latency of a 401 sitting beside a load
            # p50 of real responses.
            target.metrics_json = _dump({"status": response.status_code, "response_ms": elapsed_ms})
            target.performance_outcome = DomainOutcome.CLEAN

        target.status = NonfunctionalChildStatus.COMPLETED
        self._save(target)


def _budget_error(domain: str) -> str:
    return f"The per-URL time budget ran out before {domain} could be examined."


def _dump(data) -> str:
    import json

    try:
        return json.dumps(data)
    except (TypeError, ValueError):
        return "{}"


def _store_screenshot(
    run: NonfunctionalRun, target: NonfunctionalTarget, png: bytes, session: Session
) -> str | None:
    """Persist one target's page image. Never fatal — evidence is optional."""
    sprint = run.sprint
    if sprint is None:
        return None
    try:
        return StorageService().store_screenshot(
            png, sprint.directory, "nonfunctional", target.id, 0
        )
    except OSError as exc:
        logger.warning("Could not store target screenshot: %s", exc)
        return None


# ── the task ──────────────────────────────────────────────────────────


def run_nonfunctional_task(nonfunctional_run_id: int) -> None:
    """Walk one requirement's feature, examine what it reaches, then load-test."""
    with new_session() as session:
        run = session.get(NonfunctionalRun, nonfunctional_run_id)
        if run is None:
            logger.info("Nonfunctional run %d no longer exists — skipping", nonfunctional_run_id)
            return
        if run.status not in (NonfunctionalRunStatus.PENDING, NonfunctionalRunStatus.RUNNING):
            logger.info(
                "Nonfunctional run %d is '%s' — skipping stale job",
                nonfunctional_run_id,
                run.status,
            )
            return

        requirement = run.requirement
        sprint = requirement.sprint if requirement is not None else None
        # Deleted requirement and finished sprint share a disposition but
        # not a cause — name the right one.
        deleted = requirement is not None and requirement.archived
        if deleted or sprint is None or not sprint.active:
            finalization.fail_row(
                session,
                finalization.NONFUNCTIONAL_RUN_SPEC,
                run,
                REQUIREMENT_DELETED_ERROR if deleted else SPRINT_FINISHED_ERROR,
            )
            logger.info(
                "Nonfunctional run %d: %s — marked failed",
                nonfunctional_run_id,
                "requirement deleted" if deleted else "sprint inactive",
            )
            return

        test_env = sprint.test_environment
        env_vars = test_env.env_vars if test_env else None
        if not env_vars:
            finalization.fail_row(
                session, finalization.NONFUNCTIONAL_RUN_SPEC, run, _ENV_VARS_MISSING_ERROR
            )
            logger.warning("Nonfunctional run %d: env vars missing — failed", nonfunctional_run_id)
            return

        base_urls = [env_vars[name] for name in run.base_url_env_vars if name in env_vars]
        if not base_urls:
            finalization.fail_row(
                session, finalization.NONFUNCTIONAL_RUN_SPEC, run, _NO_BASE_URL_ERROR
            )
            logger.warning(
                "Nonfunctional run %d: no usable base URL — failed", nonfunctional_run_id
            )
            return

        run.status = NonfunctionalRunStatus.RUNNING
        run.last_heartbeat = finalization.now()
        run.updated_at = finalization.now()
        session.add(run)
        session.commit()

        try:
            # Resolve project context BEFORE any browser exists. resolve_readme
            # runs an asyncio loop, and Playwright's sync API refuses to operate
            # while one is running in the same thread — this ordering is
            # load-bearing, not stylistic.
            readme = asyncio.run(resolve_readme(sprint))
            file_tree = sprint.repo.file_tree if sprint.repo else None
            covered = [
                TestCaseLike(
                    title=case.title,
                    preconditions=case.preconditions,
                    steps=case.steps,
                    expected_result=case.expected_result,
                    case_type=case.case_type,
                    priority=case.priority,
                )
                for case in (
                    requirement.test_plan.cases if requirement.test_plan is not None else []
                )
            ]
            _heartbeat(session, run)

            catalogue = _Catalogue(session=session, run=run)
            cookies = _walk_the_feature(
                session=session,
                run=run,
                catalogue=catalogue,
                requirement=requirement,
                covered=covered,
                base_urls=base_urls,
                env_vars=env_vars,
                readme=readme,
                file_tree=file_tree,
            )

            if _superseded(session, run, nonfunctional_run_id):
                return

            _run_load_profiles(session, run, base_urls, env_vars, cookies)
            _persist_findings(session, run, catalogue)
            _write_summary(session, run, requirement)

            run.status = NonfunctionalRunStatus.COMPLETED
            run.last_heartbeat = None
            run.retry_count = 0
            run.updated_at = finalization.now()
            session.add(run)
            session.commit()
            logger.info("Nonfunctional run %d completed", nonfunctional_run_id)
        except Exception as exc:
            # Never re-raise: the DB retry counter, not RQ's failed registry,
            # is the recovery mechanism.
            logger.exception("Nonfunctional run %d failed", nonfunctional_run_id)
            finalization.record_failure(
                session, finalization.NONFUNCTIONAL_RUN_SPEC, nonfunctional_run_id, exc
            )
            return

        # Both calls sit after the COMPLETED commit and outside the try
        # above — see the identical placement in tasks/execute_test.py. A
        # tracker outage must cost a ticket, never re-drive a browser
        # through the whole feature again.
        #
        # Grouping before the export, because that files one ticket per
        # defect group.
        try:
            finding_grouping.assign_defect_groups(session, run)
        except Exception:
            logger.exception("Grouping findings failed for run %d", nonfunctional_run_id)

        try:
            finding_export.export_findings(session, run)
        except Exception:
            logger.exception("Exporting findings failed for run %d", nonfunctional_run_id)


def _superseded(session: Session, run: NonfunctionalRun, run_id: int) -> bool:
    """Stop when an upstream artifact moved under the run.

    Checked between the walk and the load profiles, and again between
    profiles — never mid-profile, where stopping would leave traffic
    half-applied with nothing recording how much.
    """
    session.expire_all()
    reasons = run.outdated_reasons
    if not reasons:
        return False
    finalization.fail_row(session, finalization.NONFUNCTIONAL_RUN_SPEC, run, SUPERSEDED_ERROR)
    logger.info("Nonfunctional run %d superseded (%s) — stopping", run_id, ", ".join(reasons))
    return True


def _walk_the_feature(
    *,
    session: Session,
    run: NonfunctionalRun,
    catalogue: _Catalogue,
    requirement,
    covered: list[TestCaseLike],
    base_urls: list[str],
    env_vars: dict[str, str],
    readme: str | None,
    file_tree: str | None,
) -> dict[str, str]:
    """Drive the browser through the feature, then examine what it called.

    Returns the browser's cookies, read on this thread before the browser
    closes — they are what a load profile sends, and no Playwright handle
    may cross into the thread pool.
    """
    cookies: dict[str, str] = {}
    browser = browser_session.BrowserSession(
        base_urls=base_urls,
        env_vars=env_vars,
        # No findings come from the browser here: the model is not offered
        # a recording tool. The callback exists only to satisfy the
        # constructor, and a call to it would be a bug.
        on_finding=lambda record, png: None,
        on_navigated=catalogue.on_navigated,
        catalogue_timeout=NONFUNCTIONAL_CATALOGUE_TIMEOUT,
    )
    # Before `__enter__`, not inside the block. `on_navigated` is bound in
    # the constructor, so `__enter__`'s own opening navigation already fires
    # the arrival hook — and a catalogue that has no browser at that moment
    # cannot examine the landing page at all.
    catalogue.browser = browser
    with browser:
        # Seed the coverage floor: every confirmed base URL is examined
        # whether or not the model goes there.
        #
        # Ordering is load-bearing, and it is the whole of a bug this once
        # had. `__enter__` opens on base_urls[0], and *that navigation
        # examines it* — under the URL it actually landed on, which a
        # redirect makes different from the one we named. Every later URL
        # has to be navigated to instead: firing the hook for a URL the
        # browser is not on runs the catalogue against whatever page is
        # loaded and files the result under the URL that was named — wrong
        # evidence on a bug report, and the named URL silently never
        # examined, because `seen_urls` now holds it. `navigate` fires the
        # hook itself, after its own post-settle origin re-check.
        if not catalogue.seen_urls:
            # Nothing arrived, so the opening navigation failed. Retry to
            # learn *why* — `__enter__` only logs it — and to give a
            # transient failure a second chance.
            outcome = browser.navigate(base_urls[0])
            if outcome.startswith("ERROR:"):
                catalogue.record_unreachable(base_urls[0], outcome)
        for url in base_urls[1:]:
            outcome = browser.navigate(url)
            if outcome.startswith("ERROR:"):
                # A base URL we cannot reach is recorded, not skipped: the
                # coverage floor says what was examined, and a silent
                # absence is the same failure this block exists to fix.
                catalogue.record_unreachable(url, outcome)
        if len(base_urls) > 1:
            # Back to where the prompt says the walk starts. The hook
            # no-ops — this URL is already in `seen_urls`.
            browser.navigate(base_urls[0])

        result = llm.run_nonfunctional_loop(
            name=requirement.name,
            description=requirement.description,
            covered_cases=covered,
            base_urls=base_urls,
            env_var_names=list(env_vars.keys()),
            readme=readme,
            file_tree=file_tree,
            tools=browser.nonfunctional_tool_registry(),
            max_actions=NONFUNCTIONAL_MAX_ACTIONS,
            snapshot_window=EXPLORATORY_SNAPSHOT_WINDOW,
            on_round=lambda actions: _heartbeat(session, run),
            # Backstop only — fill_secret already keeps values out of the
            # conversation. This run's own base URLs are kept: see
            # `redactable_items` for why the three exits differ on that.
            secrets=redactable_items(env_vars, keep=base_urls),
        )
        logger.info(
            "Nonfunctional run %d: walk finished (%s, %d actions)",
            run.id,
            result.stop_reason,
            result.actions_used,
        )

        endpoints = list(browser.discovered_endpoints)
        cookies = browser.cookies_for_load()

    # Outside the browser: endpoints are plain HTTP, and holding the browser
    # open through them would keep a Chromium process alive for nothing.
    for url in endpoints:
        catalogue.examine_endpoint(url, cookies)
        _heartbeat(session, run)
    return cookies


def _run_load_profiles(
    session: Session,
    run: NonfunctionalRun,
    base_urls: list[str],
    env_vars: dict[str, str],
    cookies: dict[str, str],
) -> None:
    """Apply each approved profile, safe methods first.

    **A profile that already sent traffic is never re-sent.**  The check is
    ``requests_sent > 0``, not the status: a restart re-pends the run and
    could legitimately re-walk its targets, but re-issuing requests against
    somebody's environment — writes included, for a non-safe method — is not
    something a retry may do on its own.

    Safe-first so that if a run is interrupted part-way the reads have
    happened and the writes have not.
    """
    profiles = session.exec(
        select(NonfunctionalLoadProfile)
        .where(NonfunctionalLoadProfile.nonfunctional_run_id == run.id)
        .order_by(NonfunctionalLoadProfile.position)
    ).all()
    ordered = sorted(profiles, key=lambda p: 0 if LoadMethod.is_safe(p.method) else 1)
    allowed = load_runner.allowed_origins_for(base_urls)

    for profile in ordered:
        if profile.requests_sent > 0:
            logger.info(
                "Load profile %d already sent %d request(s) — not re-sending",
                profile.id,
                profile.requests_sent,
            )
            continue
        if profile.status in (
            NonfunctionalChildStatus.COMPLETED,
            NonfunctionalChildStatus.ERROR,
        ):
            continue
        if _superseded(session, run, run.id):
            return

        profile.status = NonfunctionalChildStatus.RUNNING
        profile.updated_at = finalization.now()
        session.add(profile)
        session.commit()

        result = load_runner.run_profile(
            url=profile.url,
            method=profile.method,
            body=profile.body,
            cookies=cookies,
            concurrency=profile.concurrency,
            duration_seconds=profile.duration_seconds,
            total_request_cap=profile.total_request_cap,
            env_vars=env_vars,
            environment_disposable=run.environment_disposable,
            allowed_origins=allowed,
        )

        profile.requests_sent = result.requests_sent
        profile.results_json = result.to_json()
        profile.error = result.refused
        profile.status = (
            NonfunctionalChildStatus.ERROR
            if result.refused is not None
            else NonfunctionalChildStatus.COMPLETED
        )
        profile.updated_at = finalization.now()
        session.add(profile)
        _heartbeat(session, run)


def _persist_findings(session: Session, run: NonfunctionalRun, catalogue: _Catalogue) -> None:
    """Write one finding per distinct violation, triaged for readability.

    Severity comes from the violation — axe's ``impact`` or the passive
    table — and never from the triage call, whose schema has no field for
    one.  A triage failure therefore costs the prose and nothing else: every
    violation arrives carrying complete deterministic text.
    """
    violations = catalogue.violations.all()
    if not violations:
        return

    targets = {target.url: target for target in run.targets}
    triaged = llm.triage_nonfunctional_findings(
        [
            ViolationLike(
                id=f"{index}",
                domain=violation.domain,
                rule=violation.rule,
                url=violation.url,
                summary=violation.summary,
                nodes=list(violation.nodes),
            )
            for index, violation in enumerate(violations)
        ],
        # Each chunk is its own completion; without this a multi-chunk
        # triage can out-wait HEARTBEAT_STALE_SECONDS and have the whole
        # run re-enqueued as a crashed worker.
        on_attempt=lambda: _heartbeat(session, run),
    )

    position = 0
    for index, violation in enumerate(violations):
        target = targets.get(violation.url)
        if target is None:
            logger.warning("Violation at %s has no target row — skipping", violation.url)
            continue
        written = triaged.get(f"{index}")
        finding = NonfunctionalFinding(
            nonfunctional_target_id=target.id,
            position=position,
            domain=violation.domain,
            rule=violation.rule,
            finding_type=violation.finding_type,
            # The tool's verdict, never the model's.
            severity=violation.severity,
            title=written.title if written else violation.title,
            steps_to_reproduce=(
                written.steps_to_reproduce if written else violation.steps_to_reproduce
            ),
            expected=written.expected if written else violation.expected,
            actual=written.actual if written else violation.actual,
            screenshot_path=catalogue.screenshots.get(target.id),
            environment=browser_environment(None, None, violation.url),
        )
        session.add(finding)
        position += 1
    session.commit()
    logger.info("Nonfunctional run %d: %d finding(s) recorded", run.id, position)


def _write_summary(session: Session, run: NonfunctionalRun, requirement) -> None:
    """Best-effort run summary.

    A failure here logs and leaves ``summary`` null rather than failing the
    run: the findings and the measurements are the deliverable, and the user
    can retry from the run page.

    The run is still ``running``, so each attempt heartbeats — otherwise a
    retried summary could out-wait ``HEARTBEAT_STALE_SECONDS`` and have the
    reconciler re-enqueue the whole run as a crashed worker.
    """
    session.refresh(run)
    try:
        result = llm.summarize_nonfunctional(
            name=requirement.name,
            description=requirement.description,
            targets=target_summaries(run),
            load_profiles=load_profile_summaries(run),
            on_attempt=lambda: _heartbeat(session, run),
        )
    except llm.LLMError as exc:
        logger.warning("Nonfunctional run %d: summary unavailable: %s", run.id, exc)
        return

    run.summary = result.summary
    session.add(run)
    session.commit()
