import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.config import CORS_ORIGINS, STORAGE_LOCATION, STORE_OFFLINE
from backend.models.types import HealthResponse
from backend.routes.upload import router as upload_router

logger = logging.getLogger(__name__)


def _check_storage_health() -> str:
    """Determine storage readiness without creating a StorageService instance.

    Returns a human-readable status string suitable for the health endpoint.
    """
    if not STORE_OFFLINE:
        return "memory_only"
    if not STORAGE_LOCATION:
        return "unavailable: STORAGE_LOCATION is not configured"
    try:
        os.makedirs(STORAGE_LOCATION, exist_ok=True)
    except OSError as exc:
        return f"unavailable: {exc}"
    return "available"


def create_app() -> FastAPI:
    # ------------------------------------------------------------------
    # Structured logging
    # ------------------------------------------------------------------
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    app = FastAPI(title="QA Agent Backend", version="0.1.0")

    # ------------------------------------------------------------------
    # CORS (configurable via CORS_ORIGINS env var)
    # ------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(upload_router, prefix="/api")

    # ------------------------------------------------------------------
    # Global exception handler — catches unexpected errors only.
    # FastAPI's own HTTPException is re-raised so the framework returns
    # the correct status code and detail message.
    # ------------------------------------------------------------------
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        if isinstance(exc, StarletteHTTPException):
            raise exc
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------
    @app.get("/api/health", response_model=HealthResponse)
    async def health_check():
        storage = _check_storage_health()
        return HealthResponse(status="ok", storage=storage)

    return app


app = create_app()


def cli():
    """Entry point for the ``qa-agent`` console script."""
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    cli()
