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

load_dotenv(find_dotenv())

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting keep-automation-api (skeleton)")
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
