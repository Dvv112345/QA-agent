import os


def _get_bool(key: str, default: bool = False) -> bool:
    """Read an env var as a strict boolean: only the exact string ``"true"`` is truthy."""
    value = os.environ.get(key, "").strip().lower()
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
MAX_ZIP_FILES: int = _get_int("MAX_ZIP_FILES", 10_000)
MAX_TREE_DEPTH: int = _get_int("MAX_TREE_DEPTH", 100)
CORS_ORIGINS: list[str] = _get_list("CORS_ORIGINS", ["http://localhost:5173"])
VERSION: str = os.environ.get("VERSION", "0.1.0")
