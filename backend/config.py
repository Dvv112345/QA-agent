import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _get_bool(key: str, default: bool = False) -> bool:
    """Read an env var as a strict boolean: only the exact string ``"true"`` is truthy."""
    value = os.environ.get(key, "").strip().lower()
    if not value:
        return default
    return value == "true"


def _get_optional_path(key: str, default: str | None = None) -> str | None:
    """Read an env var as a filesystem path.

    When the variable is unset and *default* is provided, normalise and
    return *default*.  Otherwise returns ``None``.
    """
    value = os.environ.get(key, "").strip()
    if not value:
        if default is not None:
            return os.path.normpath(default)
        return None
    return os.path.normpath(value)


def _get_int(key: str, default: int) -> int:
    """Read an env var as an integer, returning *default* when unset or invalid."""
    value = os.environ.get(key, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning(f"Environment variable {key} cannot be loaded, using default of {default}")
        return default


def _get_float(key: str, default: float) -> float:
    """Read an env var as a float, returning *default* when unset or invalid."""
    value = os.environ.get(key, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning(f"Environment variable {key} cannot be loaded, using default of {default}")
        return default


def _get_list(key: str, default: list[str]) -> list[str]:
    """Read an env var as a comma-separated list of strings."""
    value = os.environ.get(key, "").strip()
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


STORE_OFFLINE: bool = _get_bool("STORE_OFFLINE")
STORAGE_LOCATION: str = _get_optional_path("STORAGE_LOCATION", default="./uploads")  # type: ignore[assignment]
MAX_UPLOAD_SIZE_MB: int = _get_int("MAX_UPLOAD_SIZE_MB", 100)
CORS_ORIGINS: list[str] = _get_list("CORS_ORIGINS", ["http://localhost:5173"])
VERSION: str = os.environ.get("VERSION", "0.1.0")
APP_PASSWORD: str | None = os.environ.get("APP_PASSWORD") or None

# ── PostgreSQL ────────────────────────────────────────────────────────
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/qa_agent"
)

# ── Encryption ────────────────────────────────────────────────────────
ENCRYPTION_KEY: str = os.environ.get("ENCRYPTION_KEY", "")

# ── GitHub API ────────────────────────────────────────────────────────
GITHUB_API_TIMEOUT: int = _get_int("GITHUB_API_TIMEOUT", 15)
FILE_TREE_MAX_CHARS: int = _get_int("FILE_TREE_MAX_CHARS", 20000)

# ── Redis / RQ ────────────────────────────────────────────────────────
REDIS_HOST: str = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT: int = _get_int("REDIS_PORT", 6379)
REDIS_PASSWORD: str | None = os.environ.get("REDIS_PASSWORD") or None
REDIS_DB: int = _get_int("REDIS_DB", 0)
JOB_TIMEOUT: int = _get_int("JOB_TIMEOUT", 300)
JOB_RESULT_TTL: int = _get_int("JOB_RESULT_TTL", 3600)
# RQ's idle BLPOP wait is (WORKER_TTL - 15). A blocking socket call can't be
# interrupted by Ctrl+C on Windows, so this bounds worst-case shutdown latency.
WORKER_TTL: int = _get_int("WORKER_TTL", 30)

# ── Requirement analysis ──────────────────────────────────────────────
MAX_CLARIFICATION_ROUNDS: int = _get_int("MAX_CLARIFICATION_ROUNDS", 3)
MAX_AUTO_RETRIES: int = _get_int("MAX_AUTO_RETRIES", 3)

# ── PRD upload ────────────────────────────────────────────────────────
# Char cap on the text extracted from an uploaded PRD (over the cap the
# upload is rejected, never truncated — a silently clipped PRD would
# silently drop requirements).
PRD_MAX_CHARS: int = _get_int("PRD_MAX_CHARS", 50000)
# Max requirements a single PRD split may produce.
MAX_PRD_REQUIREMENTS: int = _get_int("MAX_PRD_REQUIREMENTS", 50)

# ── Test environment analysis ─────────────────────────────────────────
MAX_TEST_ENV_REVISION_ROUNDS: int = _get_int("MAX_TEST_ENV_REVISION_ROUNDS", 3)

# ── Test planning ─────────────────────────────────────────────────────
MAX_TEST_PLAN_FEEDBACK_ROUNDS: int = _get_int("MAX_TEST_PLAN_FEEDBACK_ROUNDS", 3)
# Plan generation deliberately has no read_file tool and so no round budget:
# a plan defines what "correct" means, and reading the implementation is
# where that judgment drifts into describing what the code already does.
# RQ job_timeout for plan jobs. Generation is a single LLM call bounded by
# OPENAI_TIMEOUT, so this is vestigial headroom rather than a sized budget —
# kept because it costs nothing and plan jobs may grow again.
TEST_PLAN_JOB_TIMEOUT: int = _get_int("TEST_PLAN_JOB_TIMEOUT", 900)

# ── Exploratory testing ───────────────────────────────────────────────
# The SBTM time box, measured in LLM tool rounds rather than wall clock.
# This is the main lever on a run's duration: charters run serially, so a
# run takes the sum of its sessions with no parallelism anywhere.
# Not the whole ceiling: record_finding is free for its first
# EXPLORATORY_MAX_FINDINGS calls, so a session runs at most
# MAX_ACTIONS + MAX_FINDINGS rounds.
EXPLORATORY_MAX_ACTIONS: int = _get_int("EXPLORATORY_MAX_ACTIONS", 25)
# Cap on charters per run (also enforced on user-edited charter lists).
EXPLORATORY_MAX_CHARTERS: int = _get_int("EXPLORATORY_MAX_CHARTERS", 6)
# Per-snapshot character cap — an ARIA snapshot of a large SPA is unbounded.
EXPLORATORY_SNAPSHOT_MAX_CHARS: int = _get_int("EXPLORATORY_SNAPSHOT_MAX_CHARS", 20000)
# How many recent snapshots stay verbatim in the conversation; older ones are
# replaced with a one-line placeholder. Snapshots are the only large item in
# the loop, and a ref from many actions ago is stale anyway.
EXPLORATORY_SNAPSHOT_WINDOW: int = _get_int("EXPLORATORY_SNAPSHOT_WINDOW", 3)
# Prompt-token size at which a session compacts its own history into a summary
# before the next round. A backstop, not a routine step: at the default action
# cap a session lands around 11-21k, so this normally never fires. It cannot
# push below the floor of system prompt + charter + the verbatim snapshot
# window — those snapshots hold the refs the model is about to act on — so
# SNAPSHOT_WINDOW and SNAPSHOT_MAX_CHARS are the levers for that floor.
EXPLORATORY_CONTEXT_TOKEN_LIMIT: int = _get_int("EXPLORATORY_CONTEXT_TOKEN_LIMIT", 40000)
# Wall-clock timeout for a single Playwright action. Kept well below
# Playwright's 30 s default: an exploratory action that takes 30 s is usually
# itself the finding, and a stale element ref burns this whole budget before
# erroring.
EXPLORATORY_ACTION_TIMEOUT: int = _get_int("EXPLORATORY_ACTION_TIMEOUT", 10)
# Display only — feeds the pre-run duration estimate shown on the confirm
# button. Bounds nothing at runtime, so being wrong costs an inaccurate label.
EXPLORATORY_SECONDS_PER_ACTION: int = _get_int("EXPLORATORY_SECONDS_PER_ACTION", 8)
# Max findings one session may record, guarding against a runaway model.
# Doubles as the free-recording budget in run_exploration_loop: recording
# does not spend an action while under this cap, and does once past it —
# a free non-terminal tool would remove the loop's termination guarantee.
EXPLORATORY_MAX_FINDINGS: int = _get_int("EXPLORATORY_MAX_FINDINGS", 20)
# RQ job_timeout for exploratory runs. Must cover every charter serially:
# MAX_CHARTERS × (MAX_ACTIONS + MAX_FINDINGS) × ACTION_TIMEOUT plus the
# summary call — free recordings are extra rounds, so they count here too.
EXPLORATORY_JOB_TIMEOUT: int = _get_int("EXPLORATORY_JOB_TIMEOUT", 7200)
# Headed mode is for local debugging — watching the agent explore.
EXPLORATORY_HEADLESS: bool = _get_bool("EXPLORATORY_HEADLESS", True)

# ── Issue tracker (Jira / GitHub Issues) ──────────────────────────────
# Seconds for a single outbound tracker request (verify, create, state
# check, attachment). Deliberately the only knob here: there is no export
# cap, because grouping is what bounds how many tickets a run can file.
ISSUE_TRACKER_TIMEOUT: int = _get_int("ISSUE_TRACKER_TIMEOUT", 15)

# ── Reconciler ────────────────────────────────────────────────────────
RECONCILER_INTERVAL: int = _get_int("RECONCILER_INTERVAL", 30)
# Must exceed OPENAI_TIMEOUT so a slow LLM call is never mistaken for a dead worker.
HEARTBEAT_STALE_SECONDS: int = _get_int("HEARTBEAT_STALE_SECONDS", 180)
# Age in seconds after which a pending row's "started" RQ job counts as a
# crashed worker (died before flipping the row to analyzing). The flip is a
# single fast commit under normal operation, so this is intentionally much
# shorter than HEARTBEAT_STALE_SECONDS (which tolerates a slow LLM call).
PENDING_JOB_STALE_SECONDS: int = _get_int("PENDING_JOB_STALE_SECONDS", 30)

# ── Test execution ────────────────────────────────────────────────────
# Additional LLM-authored fix attempts per test case before a stubborn
# script-bug verdict is given up on (marked "error", not "failed").
MAX_SCRIPT_FIX_ROUNDS: int = _get_int("MAX_SCRIPT_FIX_ROUNDS", 3)
# Max read_file tool rounds per test-script generation/diagnosis call
# before a forced final answer. This is the only stage with repo access:
# it resolves the endpoint paths, parameters, and response shapes a script
# needs, which is also why diagnosing a failing script gets room to look
# around.
TEST_EXECUTION_TOOL_ROUNDS: int = _get_int("TEST_EXECUTION_TOOL_ROUNDS", 5)
# Per-file character cap for repo files fetched by that tool loop.
TEST_EXECUTION_FILE_MAX_CHARS: int = _get_int("TEST_EXECUTION_FILE_MAX_CHARS", 20000)
# Wall-clock timeout (seconds) for a single test-script subprocess run.
SCRIPT_EXECUTION_TIMEOUT: int = _get_int("SCRIPT_EXECUTION_TIMEOUT", 60)
# RQ job_timeout for test-execution jobs. Must cover many cases, each with
# up to (1 + MAX_SCRIPT_FIX_ROUNDS) generate/execute/diagnose cycles.
TEST_EXECUTION_JOB_TIMEOUT: int = _get_int("TEST_EXECUTION_JOB_TIMEOUT", 3600)

# ── CI/CD export ──────────────────────────────────────────────────────
# Max read_file tool rounds per CI/CD integration call. The model reads the
# repository to match its conventions, which is the whole task here — the
# "read_file becomes the oracle" hazard does not apply, because this call
# judges nothing about the product.
CICD_TOOL_ROUNDS: int = _get_int("CICD_TOOL_ROUNDS", 5)
# RQ job_timeout for one export: one LLM call plus five GitHub requests.
CICD_EXPORT_JOB_TIMEOUT: int = _get_int("CICD_EXPORT_JOB_TIMEOUT", 1800)
# How many workflow files an export fetches and parses before it stops
# looking. Bounds both the request count and the prompt size.
CICD_MAX_WORKFLOWS: int = _get_int("CICD_MAX_WORKFLOWS", 20)

# ── Nonfunctional testing (accessibility, performance + load, security) ──
# How many URLs one run examines. The catalogue runs at every target, so
# this bounds both the wall clock and the triage payload. Reaching it stops
# *new targets* being created, not the navigation loop: the itinerary may
# still need to walk through pages to reach something, and a hard stop
# would strand it mid-flow.
NONFUNCTIONAL_MAX_TARGETS: int = _get_int("NONFUNCTIONAL_MAX_TARGETS", 10)
# Navigation tool rounds per run — the loop's termination bound. The model
# navigates and nothing else here: findings come from the tools, so there
# is no free "record" round to budget for as there is in exploratory.
NONFUNCTIONAL_MAX_ACTIONS: int = _get_int("NONFUNCTIONAL_MAX_ACTIONS", 30)
# Findings persisted per run, across every target and domain.
NONFUNCTIONAL_MAX_FINDINGS: int = _get_int("NONFUNCTIONAL_MAX_FINDINGS", 50)
# Per-target cap on the axe payload before anything reaches a prompt.
NONFUNCTIONAL_AXE_MAX_CHARS: int = _get_int("NONFUNCTIONAL_AXE_MAX_CHARS", 20000)
# Cap on one *batched* triage call. The per-target cap alone allows
# MAX_TARGETS × AXE_MAX_CHARS in a single request, which would truncate or
# time out — and because every violation carries deterministic fallback
# text, that failure is silent: the LLM half of the feature would simply
# stop running. Above this the batch is chunked.
NONFUNCTIONAL_TRIAGE_MAX_CHARS: int = _get_int("NONFUNCTIONAL_TRIAGE_MAX_CHARS", 60000)
# Wall clock for one target's whole catalogue (axe + headers + metrics).
# Without it the itinerary half of NONFUNCTIONAL_JOB_TIMEOUT is unbounded:
# the profile term is capped by config, but axe on an arbitrary page is
# not. A domain this cuts off records `failed_to_run` — never silence.
NONFUNCTIONAL_CATALOGUE_TIMEOUT: int = _get_int("NONFUNCTIONAL_CATALOGUE_TIMEOUT", 30)
# RQ job_timeout for one nonfunctional run: the itinerary
# (MAX_TARGETS × CATALOGUE_TIMEOUT = 5 min) plus every load profile
# serially (MAX_LOAD_PROFILES × LOAD_MAX_DURATION_SECONDS) plus triage.
NONFUNCTIONAL_JOB_TIMEOUT: int = _get_int("NONFUNCTIONAL_JOB_TIMEOUT", 5400)
# Load profiles per run.
NONFUNCTIONAL_MAX_LOAD_PROFILES: int = _get_int("NONFUNCTIONAL_MAX_LOAD_PROFILES", 3)
# ── Load ceilings, two tiers ──
# Safe methods (GET/HEAD/OPTIONS) read but do not change the application,
# so they run against any confirmed origin under the first tier. Non-safe
# methods change data and run only on a run carrying the
# disposable-environment declaration, under the second, much lower tier —
# the binding cap there is the total, deliberately in the *tens*.
NONFUNCTIONAL_LOAD_MAX_CONCURRENCY: int = _get_int("NONFUNCTIONAL_LOAD_MAX_CONCURRENCY", 10)
NONFUNCTIONAL_LOAD_MAX_DURATION_SECONDS: int = _get_int(
    "NONFUNCTIONAL_LOAD_MAX_DURATION_SECONDS", 60
)
NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS: int = _get_int("NONFUNCTIONAL_LOAD_MAX_TOTAL_REQUESTS", 2000)
NONFUNCTIONAL_LOAD_UNSAFE_MAX_CONCURRENCY: int = _get_int(
    "NONFUNCTIONAL_LOAD_UNSAFE_MAX_CONCURRENCY", 2
)
NONFUNCTIONAL_LOAD_UNSAFE_MAX_TOTAL_REQUESTS: int = _get_int(
    "NONFUNCTIONAL_LOAD_UNSAFE_MAX_TOTAL_REQUESTS", 20
)
# Seconds for one outbound load request.
NONFUNCTIONAL_LOAD_REQUEST_TIMEOUT: int = _get_int("NONFUNCTIONAL_LOAD_REQUEST_TIMEOUT", 15)
# Error rate above which a profile stops early rather than keeping traffic
# on a host that is already failing.
NONFUNCTIONAL_LOAD_ERROR_RATE_STOP: float = _get_float("NONFUNCTIONAL_LOAD_ERROR_RATE_STOP", 0.5)

# ── LLM (OpenAI-compatible; DeepSeek by default) ─────────────────────
OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL: str | None = os.environ.get("OPENAI_BASE_URL") or None
OPENAI_MODEL: str = os.environ.get("OPENAI_MODEL", "deepseek-v4-flash")
OPENAI_TIMEOUT: int = _get_int("OPENAI_TIMEOUT", 60)
# Char cap on the README text included in LLM prompts (READMEs can be arbitrarily long).
README_MAX_CHARS: int = _get_int("README_MAX_CHARS", 8000)
