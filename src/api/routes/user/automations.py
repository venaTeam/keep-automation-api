"""User-tier authoring routes (D13): CRUD + alert-schema fields.

Thin layer: parse input, run the sync BL in the threadpool (blocking DNS/DB/git
I/O never touches the event loop — pattern pinned in src/bl/ssrf.py), return the
result. Domain exceptions are NOT caught here — `src/main.py` registers one
handler per exception, so the status and body for a failure live in a single
place and a new route cannot forget a branch:

- AutomationValidationError -> 400 with the accumulating machine-readable
  error list (automation-contracts.md §Validation errors)
- AutomationNotFoundError   -> 404
- AutomationBuildingError   -> 409 (mid-build submission lock, spec §8.1)

Every route passes `entity.tenant_id` into the BL: the caller's tenant comes
from the session, never from the request body (spec §4.1/§8.1). An id owned by
another tenant surfaces as AutomationNotFoundError -> 404, so the 404 branch is
doing double duty — unknown id and cross-tenant id are indistinguishable by
design.
"""
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from src.api.deps import get_authenticated_entity, get_git_client
from src.bl import automations_bl
from src.bl.git_client import GitClient
from src.models.field_allowlist import MATCHABLE_OPTIONAL, MATCHABLE_REQUIRED
from src.models.api.automation import AutomationIn, AutomationListItem, AutomationOut
from src.models.api.identity import AuthenticatedEntity
from src.models.db.automation import Automation, BuildState, MatchingState

router = APIRouter(dependencies=[Depends(get_authenticated_entity)])


def _detail(automation: Automation, script: str | None) -> dict:
    out = AutomationOut.from_orm(automation)
    out.script = script
    return out.dict()


@router.get("/automations", status_code=200)
async def list_automations(
    namespace: str | None = None,
    matching_state: MatchingState | None = None,
    build_state: BuildState | None = None,
    entity: AuthenticatedEntity = Depends(get_authenticated_entity),
):
    automations = await run_in_threadpool(
        automations_bl.list_automations,
        entity.tenant_id,
        namespace,
        matching_state,
        build_state,
    )
    return {
        "automations": [AutomationListItem.from_orm(a).dict() for a in automations]
    }


@router.post("/automations", status_code=201)
async def create_automation(
    data: AutomationIn,
    entity: AuthenticatedEntity = Depends(get_authenticated_entity),
    git: GitClient = Depends(get_git_client),
):
    automation = await run_in_threadpool(
        automations_bl.create_automation,
        entity.tenant_id,
        data,
        entity.email,
        git,
    )
    return _detail(automation, data.script)


@router.get("/automations/{automation_id}", status_code=200)
async def get_automation(
    automation_id: UUID,
    entity: AuthenticatedEntity = Depends(get_authenticated_entity),
    git: GitClient = Depends(get_git_client),
):
    automation, script = await run_in_threadpool(
        automations_bl.get_automation, entity.tenant_id, automation_id, git
    )
    return _detail(automation, script)


@router.put("/automations/{automation_id}", status_code=200)
async def update_automation(
    automation_id: UUID,
    data: AutomationIn,
    entity: AuthenticatedEntity = Depends(get_authenticated_entity),
    git: GitClient = Depends(get_git_client),
):
    automation = await run_in_threadpool(
        automations_bl.update_automation,
        entity.tenant_id,
        automation_id,
        data,
        entity.email,
        git,
    )
    return _detail(automation, data.script)


@router.get("/namespaces", status_code=200)
async def list_namespaces():
    # Stub until F25 (wallet onboarding) — see src/bl/namespaces.py.
    return {"namespaces": []}


@router.get("/alert-schema/fields", status_code=200)
async def alert_schema_fields():
    """Allowlist for trigger/cooldown dropdowns (G28 renders from this, never hardcodes)."""
    return {
        "fields": [
            {"name": name, "presence": "required"} for name in MATCHABLE_REQUIRED
        ]
        + [{"name": name, "presence": "optional"} for name in MATCHABLE_OPTIONAL]
    }
