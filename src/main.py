"""FastAPI application entrypoint for keep-automation-api.

D12 skeleton: wires the three exposure tiers (user / machine / internal) plus the
SSE stub and health check. Routes are stubs — no business logic yet (D13–D20).
"""
import logging
from contextlib import asynccontextmanager

from dotenv import find_dotenv, load_dotenv

# Before any `src` import: `src.config` reads os.environ at import time, so a
# .env loaded after it would reach os.environ but never the settings.
load_dotenv(find_dotenv())

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src import config
from src.api.ci_webhook_guard import CiWebhookGuard
from src.exceptions import (
    AutomationBuildingError,
    AutomationBusyError,
    AutomationEditSupersededError,
    AutomationLifecycleConflictError,
    AutomationNotFoundError,
    AutomationValidationError,
    BuildNotRecordedError,
    CappUnavailableError,
    LifecycleAuthenticationNotConfiguredError,
    ScriptRepoUnavailableError,
)


def resolve_log_level(name: str) -> int | None:
    """The numeric level for `name`, or None when it is not a level name."""
    level = logging.getLevelName(name.upper())
    return level if isinstance(level, int) else None


# Root handler + level. Without it Python's last-resort handler prints only
# WARNING+, so every INFO line (DB target, build cutovers) vanished in the
# container. `force` is not set: a host that configured logging first wins.
# An unknown LOG_LEVEL falls back to INFO: basicConfig would raise at import
# and crash-loop every worker over a typo.
_log_level = resolve_log_level(config.LOG_LEVEL)
logging.basicConfig(
    level=_log_level if _log_level is not None else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)
if _log_level is None:
    logger.warning("LOG_LEVEL=%r is not a logging level; using INFO", config.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting keep-automation-api (skeleton)")
    # Announce the resolved DB target once, credentials redacted. Nothing here
    # connects (the engine is lazy), so this line plus /healthcheck are the only
    # ways a wrong-DSN deploy becomes visible before requests start failing.
    from src.core import db as db_core

    logger.info(
        "Database target: %s (DSN from %s)",
        db_core.redacted_database_url(),
        config.DATABASE_URL_SOURCE,
    )
    # The CI webhook fails closed on both settings; say so once at startup so
    # a misprovisioned pod is visible before CI's first red job.
    if not config.CI_WEBHOOK_TOKEN:
        logger.warning("CI_WEBHOOK_TOKEN is not set: every CI webhook will be rejected (401)")
    from src.bl.builds import configured_image_repository

    if configured_image_repository() is None:
        logger.warning(
            "AUTOMATION_IMAGE_REPOSITORY is unset or not a valid lowercase "
            "docker repository: every successful CI build will be refused (503) "
            "and stay building"
        )
    yield
    logger.info("Shutting down keep-automation-api")


def get_app() -> FastAPI:
    app = FastAPI(
        title="Keep Automation API",
        description="Control-plane API for Keep Automations (skeleton)",
        version=config.KEEP_VERSION,
        lifespan=lifespan,
    )

    # Token + size check for the CI webhook before its body is read (D15).
    app.add_middleware(CiWebhookGuard)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_TRUSTED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Body/type failures use the same machine-readable 400 shape as the
        # domain validators (automation-contracts.md §Validation errors).
        errors = []
        for pydantic_error in exc.errors():
            location = [str(part) for part in pydantic_error["loc"] if part != "body"]
            code = (
                "field_required"
                if pydantic_error["type"].endswith("missing")
                else "invalid_value"
            )
            errors.append(
                {
                    "field": ".".join(location) or "body",
                    "code": code,
                    "message": pydantic_error["msg"],
                }
            )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"errors": errors}
        )

    # Domain exceptions are mapped once, here, rather than in per-route
    # try/except blocks: the routes stay thin (rules/automation-api.md), a new
    # route cannot forget a branch, and the status/body for a given failure is
    # defined in exactly one place. The BL raises; nothing catches in between.
    @app.exception_handler(AutomationValidationError)
    async def automation_validation_handler(
        request: Request, exc: AutomationValidationError
    ) -> JSONResponse:
        # Same accumulating shape as RequestValidationError above — the UI keys
        # off `code`, so both paths must be indistinguishable to it.
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"errors": [e.dict() for e in exc.errors]},
        )

    @app.exception_handler(AutomationNotFoundError)
    async def automation_not_found_handler(
        request: Request, exc: AutomationNotFoundError
    ) -> JSONResponse:
        # Also the cross-tenant answer: an id owned by another tenant is a 404,
        # never a 403, so existence never leaks (spec §8.1).
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "Automation not found"},
        )

    @app.exception_handler(AutomationBuildingError)
    async def automation_building_handler(
        request: Request, exc: AutomationBuildingError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "Automation is mid-build; retry after the build completes"
            },
        )

    @app.exception_handler(AutomationLifecycleConflictError)
    async def automation_lifecycle_conflict_handler(
        request: Request, exc: AutomationLifecycleConflictError
    ) -> JSONResponse:
        # Raised only after the tenant-scoped lookup succeeded, so the 409 never
        # confirms another tenant's row exists.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": (
                    "Automation is being deleted or was deleted; "
                    "this transition is no longer allowed"
                ),
                "matching_state": exc.matching_state,
            },
        )

    @app.exception_handler(BuildNotRecordedError)
    async def build_not_recorded_handler(
        request: Request, exc: BuildNotRecordedError
    ) -> JSONResponse:
        # CAPP accepted the image but the DB write failed: the build is still
        # `building`, so a redelivery completes it. Cause text never echoed.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "1"},
            content={"detail": "Build result could not be recorded; retry shortly"},
        )

    @app.exception_handler(AutomationBusyError)
    async def automation_busy_handler(
        request: Request, exc: AutomationBusyError
    ) -> JSONResponse:
        # Transient row contention, not a conflict: a retry succeeds (§8.4).
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "1"},
            content={"detail": "Automation is busy; retry shortly"},
        )

    @app.exception_handler(LifecycleAuthenticationNotConfiguredError)
    async def lifecycle_auth_not_configured_handler(
        request: Request, exc: LifecycleAuthenticationNotConfiguredError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": (
                    "Lifecycle operations are unavailable until the identity "
                    "provider is configured"
                )
            },
        )

    @app.exception_handler(AutomationEditSupersededError)
    async def automation_edit_superseded_handler(
        request: Request, exc: AutomationEditSupersededError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": (
                    "The automation changed while this edit was being saved; "
                    "reload it and retry"
                )
            },
        )

    @app.exception_handler(ScriptRepoUnavailableError)
    async def script_repo_unavailable_handler(
        request: Request, exc: ScriptRepoUnavailableError
    ) -> JSONResponse:
        # The script repo (GitLab) timed out or errored. 503, not 500: the DB
        # transaction never committed, so no row was written — the client can
        # safely retry the same request.
        return JSONResponse(
            status_code=503,
            content={"detail": "Script repository is unavailable; retry shortly"},
        )

    @app.exception_handler(CappUnavailableError)
    async def capp_unavailable_handler(
        request: Request, exc: CappUnavailableError
    ) -> JSONResponse:
        # A CAPP deploy/rollout failed before the cutover committed: the row is
        # still `building` under its lock and the old digest keeps serving, so
        # the webhook is safe to redeliver. The exception text is never echoed
        # (it must not, but could, carry CAPP identity detail — spec §3.4).
        return JSONResponse(
            status_code=503,
            content={"detail": "Execution platform is unavailable; retry shortly"},
        )

    # Health / root
    from src.api.routes.healthcheck import router as healthcheck_router

    app.include_router(healthcheck_router, tags=["health"])

    # Tier: user-facing (existing identity/session)
    from src.api.routes.user.automations import router as automations_router
    from src.api.routes.user.lifecycle_routes import router as lifecycle_router
    from src.api.routes.events import router as events_router

    app.include_router(automations_router, tags=["user"])
    app.include_router(lifecycle_router, tags=["user"])
    app.include_router(events_router, tags=["user"])

    # Tier: machine (CI webhook — network-restricted)
    from src.api.routes.machine.ci_webhook import router as ci_webhook_router

    app.include_router(ci_webhook_router, tags=["machine"])

    # Tier: internal (consumer + reconciler — service token + network-restricted)
    from src.api.routes.internal.submit import router as submit_router

    app.include_router(submit_router, tags=["internal"])

    return app


app = get_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host=config.HOST, port=config.PORT, lifespan="on")
