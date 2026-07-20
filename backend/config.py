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
# Max read_file tool rounds per plan generation before a forced final answer.
TEST_PLAN_TOOL_ROUNDS: int = _get_int("TEST_PLAN_TOOL_ROUNDS", 8)
# Per-file character cap for repo files fetched by the tool loop.
TEST_PLAN_FILE_MAX_CHARS: int = _get_int("TEST_PLAN_FILE_MAX_CHARS", 20000)
# RQ job_timeout for plan jobs. Must cover a worst-case tool loop (up to
# TEST_PLAN_TOOL_ROUNDS + 1 LLM calls × OPENAI_TIMEOUT plus GitHub fetches),
# which JOB_TIMEOUT (sized for single-call analysis jobs) does not.
TEST_PLAN_JOB_TIMEOUT: int = _get_int("TEST_PLAN_JOB_TIMEOUT", 900)

# ── Reconciler ────────────────────────────────────────────────────────
RECONCILER_INTERVAL: int = _get_int("RECONCILER_INTERVAL", 30)
# Must exceed OPENAI_TIMEOUT so a slow LLM call is never mistaken for a dead worker.
HEARTBEAT_STALE_SECONDS: int = _get_int("HEARTBEAT_STALE_SECONDS", 180)
# Age in seconds after which a pending row's "started" RQ job counts as a
# crashed worker (died before flipping the row to analyzing). The flip is a
# single fast commit under normal operation, so this is intentionally much
# shorter than HEARTBEAT_STALE_SECONDS (which tolerates a slow LLM call).
PENDING_JOB_STALE_SECONDS: int = _get_int("PENDING_JOB_STALE_SECONDS", 30)

# ── LLM (OpenAI-compatible; DeepSeek by default) ─────────────────────
OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL: str | None = os.environ.get("OPENAI_BASE_URL") or None
OPENAI_MODEL: str = os.environ.get("OPENAI_MODEL", "deepseek-v4-flash")
OPENAI_TIMEOUT: int = _get_int("OPENAI_TIMEOUT", 60)
