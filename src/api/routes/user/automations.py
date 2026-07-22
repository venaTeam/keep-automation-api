"""User-tier authoring routes (D13): CRUD + alert-schema fields.

Thin layer: parse input, run the sync BL in the threadpool (blocking DNS/DB/git
I/O never touches the event loop — pattern pinned in src/bl/ssrf.py), map
domain exceptions to HTTP:

- AutomationValidationError -> 400 with the accumulating machine-readable
  error list (automation-contracts.md §Validation errors)
- AutomationNotFoundError   -> 404
- AutomationBuildingError   -> 409 (mid-build submission lock, spec §8.1)
"""
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from src.api.deps import get_authenticated_entity, get_git_client
from src.bl import automations_bl
from src.bl.git_client import GitClient
from src.contracts.field_allowlist import MATCHABLE_OPTIONAL, MATCHABLE_REQUIRED
from src.exceptions import (
    AutomationBuildingError,
    AutomationNotFoundError,
    AutomationValidationError,
)
from src.models.api.automation import AutomationIn, AutomationListItem, AutomationOut
from src.models.db.automation import Automation, BuildState, MatchingState

router = APIRouter(dependencies=[Depends(get_authenticated_entity)])


def _validation_error_response(exc: AutomationValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"errors": [e.dict() for e in exc.errors]},
    )


def _detail(automation: Automation, script: str | None) -> dict:
    out = AutomationOut.from_orm(automation)
    out.script = script
    return out.dict()


@router.get("/automations")
async def list_automations(
    namespace: str | None = None,
    matching_state: MatchingState | None = None,
    build_state: BuildState | None = None,
):
    automations = await run_in_threadpool(
        automations_bl.list_automations, namespace, matching_state, build_state
    )
    return {
        "automations": [AutomationListItem.from_orm(a).dict() for a in automations]
    }


@router.post("/automations", status_code=201)
async def create_automation(
    data: AutomationIn,
    entity: dict = Depends(get_authenticated_entity),
    git: GitClient = Depends(get_git_client),
):
    try:
        automation = await run_in_threadpool(
            automations_bl.create_automation, data, entity["email"], git
        )
    except AutomationValidationError as exc:
        return _validation_error_response(exc)
    return _detail(automation, data.script)


@router.get("/automations/{automation_id}")
async def get_automation(
    automation_id: UUID,
    git: GitClient = Depends(get_git_client),
):
    try:
        automation, script = await run_in_threadpool(
            automations_bl.get_automation, automation_id, git
        )
    except AutomationNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "Automation not found"})
    return _detail(automation, script)


@router.put("/automations/{automation_id}")
async def update_automation(
    automation_id: UUID,
    data: AutomationIn,
    entity: dict = Depends(get_authenticated_entity),
    git: GitClient = Depends(get_git_client),
):
    try:
        automation = await run_in_threadpool(
            automations_bl.update_automation, automation_id, data, entity["email"], git
        )
    except AutomationValidationError as exc:
        return _validation_error_response(exc)
    except AutomationNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "Automation not found"})
    except AutomationBuildingError:
        return JSONResponse(
            status_code=409,
            content={"detail": "Automation is mid-build; retry after the build completes"},
        )
    return _detail(automation, data.script)


@router.get("/namespaces")
async def list_namespaces():
    # Stub until F25 (wallet onboarding) — see src/bl/namespaces.py.
    return {"namespaces": []}


@router.get("/alert-schema/fields")
async def alert_schema_fields():
    """Allowlist for trigger/cooldown dropdowns (G28 renders from this, never hardcodes)."""
    return {
        "fields": [
            {"name": name, "presence": "required"} for name in MATCHABLE_REQUIRED
        ]
        + [{"name": name, "presence": "optional"} for name in MATCHABLE_OPTIONAL]
    }
