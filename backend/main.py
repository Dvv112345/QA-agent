import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from backend.config import CORS_ORIGINS, STORAGE_LOCATION, STORE_OFFLINE, VERSION
from backend.database import init_db
from backend.migrations import run_migrations
from backend.models.types import HealthResponse
from backend.routes.auth import router as auth_router
from backend.routes.repos import router as repos_router
from backend.routes.requirements import router as requirements_router
from backend.routes.sprints import router as sprints_router
from backend.routes.test_environment import router as test_environment_router
from backend.routes.test_execution import router as test_execution_router
from backend.routes.test_plans import router as test_plans_router

logger = logging.getLogger(__name__)


def _check_storage_health() -> str:
    """Determine storage readiness without creating a StorageService instance.

    Returns a human-readable status string suitable for the health endpoint.
    """
    if not STORE_OFFLINE:
        return "memory_only"
    try:
        os.makedirs(STORAGE_LOCATION, exist_ok=True)
    except OSError as exc:
        return f"unavailable: {exc}"
    return "available"


def _check_redis_health() -> str:
    """Check whether Redis is reachable.

    Returns a human-readable status string suitable for the health endpoint.
    """
    import redis as _redis

    from backend.config import REDIS_DB, REDIS_HOST, REDIS_PASSWORD, REDIS_PORT

    try:
        r = _redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            db=REDIS_DB,
            socket_connect_timeout=2,
        )
        r.ping()
        return "available"
    except Exception as exc:
        return f"unavailable: {exc}"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Run the reconciler loop for the app's lifetime.

    Note: httpx ``ASGITransport`` never runs lifespan, so this is inert in
    the API test suite — ``reconcile_once`` is unit-tested directly.
    """
    from backend.services.reconciler import reconciler_loop

    task = asyncio.create_task(reconciler_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    # ------------------------------------------------------------------
    # Structured logging
    # ------------------------------------------------------------------
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    app = FastAPI(title="QA Agent Backend", version=VERSION, lifespan=_lifespan)

    # ------------------------------------------------------------------
    # Database initialisation
    # ------------------------------------------------------------------
    init_db()
    run_migrations()

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

    app.include_router(auth_router, prefix="/api")
    app.include_router(repos_router, prefix="/api")
    app.include_router(sprints_router, prefix="/api")
    app.include_router(requirements_router, prefix="/api")
    app.include_router(test_environment_router, prefix="/api")
    app.include_router(test_plans_router, prefix="/api")
    app.include_router(test_execution_router, prefix="/api")

    # ------------------------------------------------------------------
    # Global exception handler — catches unexpected errors only.
    # FastAPI's own HTTPException is re-raised so the framework returns
    # the correct status code and detail message.
    # ------------------------------------------------------------------
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        if isinstance(exc, HTTPException):
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
        redis_status = _check_redis_health()
        return HealthResponse(status="ok", storage=storage, redis=redis_status)

    return app


app = create_app()


def cli():
    """Entry point for the ``qa-agent`` console script."""
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    cli()
