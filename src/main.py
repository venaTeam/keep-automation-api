"""FastAPI application entrypoint for keep-automation-api.

D12 skeleton: wires the three exposure tiers (user / machine / internal) plus the
SSE stub and health check. Routes are stubs — no business logic yet (D13–D20).
"""
import logging
from contextlib import asynccontextmanager

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src import config
from src.exceptions import (
    AutomationBuildingError,
    AutomationNotFoundError,
    AutomationValidationError,
)

load_dotenv(find_dotenv())

logger = logging.getLogger(__name__)


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
    yield
    logger.info("Shutting down keep-automation-api")


def get_app() -> FastAPI:
    app = FastAPI(
        title="Keep Automation API",
        description="Control-plane API for Keep Automations (skeleton)",
        version=config.KEEP_VERSION,
        lifespan=lifespan,
    )

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
        return JSONResponse(status_code=400, content={"errors": errors})

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
            status_code=400,
            content={"errors": [e.dict() for e in exc.errors]},
        )

    @app.exception_handler(AutomationNotFoundError)
    async def automation_not_found_handler(
        request: Request, exc: AutomationNotFoundError
    ) -> JSONResponse:
        # Also the cross-tenant answer: an id owned by another tenant is a 404,
        # never a 403, so existence never leaks (spec §8.1).
        return JSONResponse(status_code=404, content={"detail": "Automation not found"})

    @app.exception_handler(AutomationBuildingError)
    async def automation_building_handler(
        request: Request, exc: AutomationBuildingError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Automation is mid-build; retry after the build completes"
            },
        )

    # Health / root
    from src.api.routes.healthcheck import router as healthcheck_router

    app.include_router(healthcheck_router, tags=["health"])

    # Tier: user-facing (existing identity/session)
    from src.api.routes.user.automations import router as automations_router
    from src.api.routes.events import router as events_router

    app.include_router(automations_router, tags=["user"])
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
