import os
from typing import Optional


def _get_bool(key: str, default: bool = False) -> bool:
    """Read an env var as a strict boolean: only the exact string ``"true"`` is truthy."""
    value = os.environ.get(key, "").strip().lower()
    return value == "true"


def _get_optional_path(key: str) -> Optional[str]:
    """Read an env var as a filesystem path, returning ``None`` when unset."""
    value = os.environ.get(key, "").strip()
    if not value:
        return None
    return os.path.normpath(value)


STORE_OFFLINE: bool = _get_bool("STORE_OFFLINE")
STORAGE_LOCATION: Optional[str] = _get_optional_path("STORAGE_LOCATION")
