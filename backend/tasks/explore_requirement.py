"""Exploratory-testing task, executed by the RQ worker.

One job per run (``ExploratoryRun``), which covers exactly one requirement:
walks that run's approved charters in order, giving each its own live
browser and LLM tool loop, then writes a best-effort per-requirement
summary.  Job args are the run id only — everything else is read fresh from
the database, which makes every enqueue idempotent and reconciler-safe.

Charters run **sequentially**, never in parallel.  Charters for one
requirement attack the same feature by construction, so concurrent sessions
would collide on the same records and manufacture false findings ("the
record I created vanished").  Serial execution also means the run owns every
session sheet when the summary call happens — no fan-in, no coordination.

Resumability is derived entirely from each ``ExploratorySession``'s own
status: finalized sessions are skipped on a retry.  An interrupted session
restarts its charter from scratch — a half-explored browser died with the
worker and cannot be resumed — while findings it already persisted are kept,
since they were real observations regardless.

Must not import from ``backend.services.queue`` or ``backend.worker``
(circular-import rule).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from backend.config import (
    EXPLORATORY_MAX_ACTIONS,
    EXPLORATORY_SNAPSHOT_WINDOW,
    MAX_AUTO_RETRIES,
)
from backend.database import new_session
from backend.models.database import (
    REQUIREMENT_DELETED_ERROR,
    SPRINT_FINISHED_ERROR,
    SUPERSEDED_ERROR,
    ExploratoryFinding,
    ExploratoryRun,
    ExploratoryRunStatus,
    ExploratorySession,
    ExploratorySessionStatus,
    FindingSeverity,
    FindingType,
)
from backend.services import browser_session, llm
from backend.services.storage import StorageService
from backend.utils.exploratory_utils import session_sheets
from backend.utils.readme_utils import resolve_readme

logger = logging.getLogger(__name__)

# Cap for the user-facing error summary stored on a failed row.
_ERROR_SUMMARY_MAX_CHARS = 300

# Cap for the stored per-session action log — mirrors execute_test.py's
# _OUTPUT_MAX_CHARS precedent. 25 actions is a few KB, so this only bites
# on a pathological session.
_ACTION_LOG_MAX_CHARS = 20000

# Should be unreachable via normal flow — guarded per this codebase's
# convention of never trusting a supposedly-impossible state blindly.
_ENV_VARS_MISSING_ERROR = "Test environment access variables have not been established."
_NO_BASE_URL_ERROR = "No application URL is available for this run."

# Deliberately no "plan still approved" guard, unlike execute_test.py: the
# approved plan's cases are consumed once, at charter-generation time, as
# "already covered" context. Nothing mid-run depends on the plan's status.


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _record_failure(session: Session, run_id: int, exc: Exception) -> None:
    """Count the failure and either re-queue the run or mark it failed.

    Sessions already finalized before the exception stay finalized — the next
    attempt resumes from the first non-finalized charter.
    """
    session.rollback()
    run = session.get(ExploratoryRun, run_id)
    if run is None:
        return

    run.retry_count += 1
    if run.retry_count >= MAX_AUTO_RETRIES:
        run.status = ExploratoryRunStatus.FAILED
        run.error = str(exc)[:_ERROR_SUMMARY_MAX_CHARS]
    else:
        # Back to pending — the reconciler re-enqueues it.
        run.status = ExploratoryRunStatus.PENDING
    run.last_heartbeat = None
    run.updated_at = _now()
    session.add(run)
    session.commit()


def _fail_run(session: Session, run: ExploratoryRun, error: str) -> None:
    run.status = ExploratoryRunStatus.FAILED
    run.error = error
    run.last_heartbeat = None
    run.updated_at = _now()
    session.add(run)
    session.commit()


def _heartbeat(session: Session, run: ExploratoryRun) -> None:
    """Mark the run alive so a live session is never swept as a crashed worker."""
    run.last_heartbeat = _now()
    session.add(run)
    session.commit()


def _build_on_round(
    session: Session,
    run: ExploratoryRun,
    exploratory_session: ExploratorySession,
):
    """Heartbeat the run and publish the session's action count as it climbs.

    The loop's own counter is otherwise only readable once the session ends,
    which left the UI showing 0 for the whole session and then the final
    number.  Both writes share the one commit the heartbeat already made.
    """

    def on_round(actions_used: int) -> None:
        run.last_heartbeat = _now()
        session.add(run)
        exploratory_session.actions_used = actions_used
        exploratory_session.updated_at = _now()
        session.add(exploratory_session)
        session.commit()

    return on_round


def _build_on_finding(
    session: Session,
    exploratory_session: ExploratorySession,
    directory: str,
    storage: StorageService,
    counter: dict[str, int],
):
    """Persist a finding as the model records it, with its live screenshot.

    Owns the per-session ``position`` counter.  A screenshot is optional by
    design: ``store_screenshot`` returns ``None`` whenever ``STORE_OFFLINE``
    is disabled, and the finding is persisted regardless.
    """

    def on_finding(record: browser_session.FindingRecord, png: bytes | None) -> None:
        position = counter["n"]
        screenshot_path: str | None = None
        if png is not None:
            try:
                screenshot_path = storage.store_screenshot(
                    png, directory, exploratory_session.id, position
                )
            except OSError as exc:
                # An unwritable disk must not cost us the finding itself.
                logger.warning("Could not store finding screenshot: %s", exc)

        finding = ExploratoryFinding(
            exploratory_session_id=exploratory_session.id,
            position=position,
            # The tool schema constrains both of these to an enum, but the
            # model is not bound by it. An unrecognised type counts toward
            # neither bug_count nor issue_count while still counting toward
            # finding_count, so the run page would show numbers that do not
            # add up; an unrecognised severity would make
            # high_severity_count mean something different here than on the
            # scripted path.
            finding_type=FindingType.normalize(record.finding_type),
            severity=FindingSeverity.normalize(record.severity),
            title=record.title,
            steps_to_reproduce=record.steps_to_reproduce,
            expected=record.expected,
            actual=record.actual,
            screenshot_path=screenshot_path,
            environment=record.environment,
        )
        session.add(finding)
        session.commit()
        counter["n"] = position + 1

    return on_finding


def explore_requirement_task(exploratory_run_id: int) -> None:
    """Run (or resume) every charter for one requirement, then summarize."""
    with new_session() as session:
        run = session.get(ExploratoryRun, exploratory_run_id)
        if run is None:
            logger.info("Exploratory run %d no longer exists — skipping", exploratory_run_id)
            return
        if run.status not in (ExploratoryRunStatus.PENDING, ExploratoryRunStatus.RUNNING):
            logger.info(
                "Exploratory run %d is '%s' — skipping stale job", exploratory_run_id, run.status
            )
            return

        requirement = run.requirement
        sprint = requirement.sprint if requirement is not None else None
        # Deleted requirement and finished sprint share a disposition but
        # not a cause — name the right one.
        deleted = requirement is not None and requirement.archived
        if deleted or sprint is None or not sprint.active:
            _fail_run(session, run, REQUIREMENT_DELETED_ERROR if deleted else SPRINT_FINISHED_ERROR)
            logger.info(
                "Exploratory run %d: %s — marked failed",
                exploratory_run_id,
                "requirement deleted" if deleted else "sprint inactive",
            )
            return

        test_env = sprint.test_environment
        env_vars = test_env.env_vars if test_env else None
        if not env_vars:
            _fail_run(session, run, _ENV_VARS_MISSING_ERROR)
            logger.warning("Exploratory run %d: env vars missing — failed", exploratory_run_id)
            return

        base_urls = [env_vars[name] for name in run.base_url_env_vars if name in env_vars]
        if not base_urls:
            _fail_run(session, run, _NO_BASE_URL_ERROR)
            logger.warning("Exploratory run %d: no usable base URL — failed", exploratory_run_id)
            return

        run.status = ExploratoryRunStatus.RUNNING
        run.last_heartbeat = _now()
        run.updated_at = _now()
        session.add(run)
        session.commit()

        try:
            # Resolve project context BEFORE any browser exists. resolve_readme
            # runs an asyncio loop, and Playwright's sync API refuses to operate
            # while one is running in the same thread — this ordering is
            # load-bearing, not stylistic.
            readme = asyncio.run(resolve_readme(sprint))
            file_tree = sprint.repo.file_tree if sprint.repo else None
            env_var_names = list(env_vars.keys())
            storage = StorageService()

            _heartbeat(session, run)

            sessions = session.exec(
                select(ExploratorySession)
                .where(ExploratorySession.exploratory_run_id == exploratory_run_id)
                .order_by(ExploratorySession.position)
            ).all()

            for exploratory_session in sessions:
                if exploratory_session.status in (
                    ExploratorySessionStatus.COMPLETED,
                    ExploratorySessionStatus.ERROR,
                ):
                    continue  # already finalized — resumability

                with session.no_autoflush:
                    current_status = session.exec(
                        select(ExploratoryRun.status).where(ExploratoryRun.id == exploratory_run_id)
                    ).one_or_none()
                if current_status != ExploratoryRunStatus.RUNNING:
                    logger.info(
                        "Exploratory run %d changed to '%s' mid-run — stopping",
                        exploratory_run_id,
                        current_status,
                    )
                    session.rollback()
                    return

                # Stop as soon as an upstream artifact moves — see the same
                # check in execute_test.py. It matters more here: a charter
                # is a full browser session, easily the most expensive unit
                # of work in the system. Findings already recorded are kept;
                # they were real observations regardless of what changed
                # afterwards.
                session.expire_all()
                # The plan is excluded on purpose, consistent with the
                # module note above: its cases were consumed once, at
                # charter-generation time, so a plan edit does not
                # invalidate charters already written and half-explored.
                # The run is still *reported* outdated for that reason —
                # this only decides whether to keep going.
                superseding = [r for r in run.outdated_reasons if r != "test_plan"]
                if superseding:
                    _fail_run(session, run, SUPERSEDED_ERROR)
                    logger.info(
                        "Exploratory run %d superseded mid-run (%s) — stopping",
                        exploratory_run_id,
                        ", ".join(superseding),
                    )
                    return

                exploratory_session.status = ExploratorySessionStatus.RUNNING
                # A retried charter restarts from scratch, so any count left
                # behind by the attempt that died describes nothing.
                exploratory_session.actions_used = 0
                exploratory_session.updated_at = _now()
                session.add(exploratory_session)
                session.commit()

                _run_one_session(
                    session=session,
                    exploratory_session=exploratory_session,
                    requirement_name=requirement.name,
                    requirement_description=requirement.description,
                    base_urls=base_urls,
                    env_vars=env_vars,
                    env_var_names=env_var_names,
                    readme=readme,
                    file_tree=file_tree,
                    directory=sprint.directory,
                    storage=storage,
                    on_round=_build_on_round(session, run, exploratory_session),
                )

            _write_summary(session, run, requirement)

            run.status = ExploratoryRunStatus.COMPLETED
            run.last_heartbeat = None
            run.retry_count = 0
            run.updated_at = _now()
            session.add(run)
            session.commit()
            logger.info("Exploratory run %d completed", exploratory_run_id)
        except Exception as exc:
            # Never re-raise: the DB retry counter, not RQ's failed registry,
            # is the recovery mechanism.
            logger.exception("Exploratory run %d failed", exploratory_run_id)
            _record_failure(session, exploratory_run_id, exc)


def _run_one_session(
    *,
    session: Session,
    exploratory_session: ExploratorySession,
    requirement_name: str,
    requirement_description: str,
    base_urls: list[str],
    env_vars: dict[str, str],
    env_var_names: list[str],
    readme: str | None,
    file_tree: str | None,
    directory: str,
    storage: StorageService,
    on_round,
) -> None:
    """Explore one charter. Never raises — a bad charter must not abandon the run."""
    counter = {"n": 0}
    try:
        with browser_session.BrowserSession(
            base_urls=base_urls,
            env_vars=env_vars,
            on_finding=_build_on_finding(session, exploratory_session, directory, storage, counter),
        ) as browser:
            result = llm.run_exploration_loop(
                name=requirement_name,
                description=requirement_description,
                charter=exploratory_session.charter,
                sfdipot_areas=exploratory_session.sfdipot_areas,
                base_urls=base_urls,
                env_var_names=env_var_names,
                # Backstop only — fill_secret already keeps values out of the
                # conversation. The base URLs are env values too, and are
                # excluded because redacting them would gut the log while
                # protecting nothing.
                secret_values=set(env_vars.values()) - set(base_urls),
                readme=readme,
                file_tree=file_tree,
                tools=browser.tool_registry(),
                max_actions=EXPLORATORY_MAX_ACTIONS,
                snapshot_window=EXPLORATORY_SNAPSHOT_WINDOW,
                on_round=on_round,
            )
    except Exception as exc:
        logger.exception("Exploratory session %d failed", exploratory_session.id)
        # Only uncommitted state is discarded, so the per-round action counts
        # on_round already committed survive: a session that dies mid-charter
        # reports how far it actually got rather than 0.
        session.rollback()
        exploratory_session.status = ExploratorySessionStatus.ERROR
        exploratory_session.error = str(exc)[:_ERROR_SUMMARY_MAX_CHARS]
        exploratory_session.stop_reason = "error"
        exploratory_session.updated_at = _now()
        session.add(exploratory_session)
        session.commit()
        return

    exploratory_session.status = ExploratorySessionStatus.COMPLETED
    exploratory_session.session_notes = result.notes
    exploratory_session.actions_used = result.actions_used
    exploratory_session.action_log = "\n".join(result.action_log)[:_ACTION_LOG_MAX_CHARS]
    exploratory_session.stop_reason = result.stop_reason
    exploratory_session.updated_at = _now()
    session.add(exploratory_session)
    session.commit()


def _write_summary(session: Session, run: ExploratoryRun, requirement) -> None:
    """Best-effort per-requirement summary.

    Runs only on the pass that completes the session loop, so a job that
    crashed mid-loop simply never reaches it and the retry writes it once at
    the end.  A failure here logs and leaves ``summary`` null rather than
    failing the run: the findings and session sheets are the deliverable, and
    the user can retry the summary from the run page.

    The run is still ``running`` here, so each of the summary call's attempts
    heartbeats — otherwise a retried summary could out-wait
    ``HEARTBEAT_STALE_SECONDS`` and have the reconciler re-enqueue the whole
    run as a crashed worker.  Not ``_build_on_round``: that one also publishes
    a session's action count, and no session is in scope any more.
    """
    session.refresh(run)

    def heartbeat() -> None:
        run.last_heartbeat = _now()
        session.add(run)
        session.commit()

    try:
        result = llm.summarize_exploration(
            name=requirement.name,
            description=requirement.description,
            sessions=session_sheets(run),
            on_attempt=heartbeat,
        )
    except llm.LLMError as exc:
        logger.warning("Exploratory run %d: summary unavailable: %s", run.id, exc)
        return

    run.summary = result.summary
    session.add(run)
    session.commit()
