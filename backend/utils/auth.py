"""Authentication dependency for QA Agent.

Provides a FastAPI dependency that gates protected routes behind a shared
secret password stored in the ``qa_auth`` HttpOnly session cookie.

When ``APP_PASSWORD`` is not set, authentication is disabled (all requests pass).
"""

import logging
import secrets

from fastapi import HTTPException, Request

import backend.config

logger = logging.getLogger(__name__)


def verify_auth(request: Request) -> bool:
    """FastAPI dependency that validates the ``qa_auth`` cookie.

    If ``APP_PASSWORD`` is ``None`` or empty, authentication is disabled
    and every request passes through without any check.

    Otherwise the dependency reads the ``qa_auth`` cookie from the request
    and compares it against ``APP_PASSWORD`` using a constant-time
    comparison to prevent timing side-channels.

    Returns:
        ``True`` when the request is authenticated (or auth is disabled).

    Raises:
        HTTPException(401): The cookie is missing, empty, or does not match
            the configured ``APP_PASSWORD``.
    """
    password = backend.config.APP_PASSWORD

    if not password:
        return True

    cookie_value: str | None = request.cookies.get("qa_auth")

    if not cookie_value or not secrets.compare_digest(cookie_value, password):
        logger.warning("Authentication failure for %s", request.url.path)
        raise HTTPException(status_code=401, detail="Invalid or missing access code")

    return True
