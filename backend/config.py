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


def _get_optional_path(key: str) -> str | None:
    """Read an env var as a filesystem path, returning ``None`` when unset."""
    value = os.environ.get(key, "").strip()
    if not value:
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
STORAGE_LOCATION: str | None = _get_optional_path("STORAGE_LOCATION")
MAX_UPLOAD_SIZE_MB: int = _get_int("MAX_UPLOAD_SIZE_MB", 100)
MAX_ZIP_FILES: int = _get_int("MAX_ZIP_FILES", 10000)
MAX_TREE_DEPTH: int = _get_int("MAX_TREE_DEPTH", 100)
CORS_ORIGINS: list[str] = _get_list("CORS_ORIGINS", ["http://localhost:5173"])
VERSION: str = os.environ.get("VERSION", "0.1.0")
CHUNK_SIZE: int = _get_int("CHUNK_SIZE", 8192)
REDIS_HOST: str = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT: int = _get_int("REDIS_PORT", 6379)
REDIS_PASSWORD: str | None = os.environ.get("REDIS_PASSWORD") or None
REDIS_DB: int = _get_int("REDIS_DB", 0)
JOB_TIMEOUT: int = _get_int("JOB_TIMEOUT", 300)
JOB_RESULT_TTL: int = _get_int("JOB_RESULT_TTL", 3600)
