import os


def _get_bool(key: str, default: bool = False) -> bool:
    """Read an env var as a strict boolean: only the exact string ``"true"`` is truthy."""
    value = os.environ.get(key, "").strip().lower()
    return value == "true"


def _get_path(key: str, default: str = "./uploads") -> str:
    """Read an env var as a filesystem path, normalising trailing slashes."""
    value = os.environ.get(key, default).strip()
    return os.path.normpath(value)


STORE_OFFLINE: bool = _get_bool("STORE_OFFLINE")
STORAGE_LOCATION: str = _get_path("STORAGE_LOCATION", "./uploads")
