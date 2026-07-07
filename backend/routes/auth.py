"""Authentication routes — verify password and check auth status.

These routes are intentionally *not* protected by the ``verify_auth``
dependency.  ``/api/auth/verify`` sets the cookie on successful login
and ``/api/auth/check`` reports whether the current cookie is valid
(so the frontend can decide whether to show the login modal).
"""

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import backend.config
from backend.models.types import AuthCheckResponse, PasswordVerifyRequest

router = APIRouter(tags=["auth"])


@router.post("/auth/verify", response_model=AuthCheckResponse)
async def verify_password(body: PasswordVerifyRequest, request: Request):
    """Check a submitted password and set an HttpOnly session cookie on match.

    Cookie attributes (match the plan):
    - HttpOnly (JavaScript cannot read it)
    - SameSite=Strict (CSRF protection)
    - Path=/ (available to all paths)
    - No Expires/Max-Age (session cookie — cleared on browser close)
    """
    password = backend.config.APP_PASSWORD

    # ── Auth disabled ──────────────────────────────────────────────────
    if not password:
        return AuthCheckResponse(valid=True)

    # ── Verify ─────────────────────────────────────────────────────────
    if secrets.compare_digest(body.password, password):
        response = JSONResponse(content={"valid": True})
        response.set_cookie(
            key="qa_auth",
            value=password,
            path="/",
            samesite="strict",
            httponly=True,
        )
        return response

    # ── Mismatch — no cookie, just report invalid ──────────────────────
    return AuthCheckResponse(valid=False)


@router.get("/auth/check", response_model=AuthCheckResponse)
async def check_auth(request: Request):
    """Report whether the current ``qa_auth`` cookie is valid.

    This endpoint never returns 401 — it answers "am I logged in?",
    not "you must be logged in".  The frontend calls this on page load
    to decide whether to show the login modal.
    """
    password = backend.config.APP_PASSWORD

    # ── Auth disabled ──────────────────────────────────────────────────
    if not password:
        return AuthCheckResponse(valid=True)

    cookie_value: str | None = request.cookies.get("qa_auth")

    if cookie_value and secrets.compare_digest(cookie_value, password):
        return AuthCheckResponse(valid=True)

    return AuthCheckResponse(valid=False)
