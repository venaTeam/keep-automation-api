"""App-level domain-exception handlers (src/main.py).

The routes deliberately contain no try/except: each domain exception is mapped
to its status and body exactly once, in `get_app()`. These tests pin that
mapping directly — the route tests in test_automations_routes.py prove the
end-to-end behaviour, but only for the routes that exist today. A route added
later inherits the mapping for free, and if a handler is dropped these fail
even when no route happens to exercise it.

Every route is also asserted to declare an explicit `status_code`, so a new
route cannot fall back to the implicit default and leave the OpenAPI schema
lying about what it returns.
"""
import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src.models.api.validation_errors import ErrorCode, FieldError
from src.exceptions import (
    AutomationBuildingError,
    AutomationNotFoundError,
    AutomationValidationError,
)
from src.main import get_app


@pytest.fixture()
def raising_client(test_engine):
    """A client whose routes do nothing but raise each domain exception.

    Mounted on the real `get_app()`, so the handlers under test are the ones
    the service actually registers — not a reconstruction of them.
    """
    app = get_app()
    router = APIRouter()

    @router.get("/_raises/validation")
    async def _validation():
        raise AutomationValidationError(
            [
                FieldError(
                    field="triggers",
                    code=ErrorCode.TRIGGERS_TOO_FEW,
                    message="too few",
                )
            ]
        )

    @router.get("/_raises/not-found")
    async def _not_found():
        raise AutomationNotFoundError()

    @router.get("/_raises/building")
    async def _building():
        raise AutomationBuildingError()

    app.include_router(router)
    # The handlers must convert these into responses; if one is missing the
    # exception escapes as a 500 and TestClient re-raises it instead.
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_validation_error_becomes_the_accumulating_400(raising_client):
    resp = raising_client.get("/_raises/validation")
    assert resp.status_code == 400
    # Same shape as RequestValidationError — the UI keys off `code` and must not
    # be able to tell the two paths apart.
    assert resp.json() == {
        "errors": [
            {
                "field": "triggers",
                "code": ErrorCode.TRIGGERS_TOO_FEW.value,
                "message": "too few",
            }
        ]
    }


def test_not_found_becomes_404(raising_client):
    resp = raising_client.get("/_raises/not-found")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Automation not found"}


def test_building_becomes_409(raising_client):
    resp = raising_client.get("/_raises/building")
    assert resp.status_code == 409
    assert "mid-build" in resp.json()["detail"]


def test_every_route_declares_an_explicit_status_code():
    """No route may rely on the implicit default.

    FastAPI leaves `status_code=None` when the decorator omits it and infers
    200/201 at response time, which keeps it out of the OpenAPI schema — the
    thing clients generate from.
    """
    app = get_app()
    # APIRoute only: /docs, /redoc and /openapi.json are FastAPI's own
    # plain-Starlette routes, not ours to annotate.
    ours = [route for route in app.routes if isinstance(route, APIRoute)]
    assert ours, "no APIRoutes found — the audit would pass vacuously"
    missing = [
        f"{sorted(route.methods)} {route.path}"
        for route in ours
        if route.status_code is None
    ]
    assert missing == [], f"routes without an explicit status_code: {missing}"
