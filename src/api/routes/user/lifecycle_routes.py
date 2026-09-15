"""User-tier lifecycle routes (D18, spec §8.1): enable / disable.

Thin layer, same shape as `automations.py`: the tenant and actor come from the
session, the sync BL runs in the threadpool, and domain exceptions are mapped
once in `src/main.py`:

- AutomationValidationError        -> 400 (`active_digest_required`)
- AutomationNotFoundError          -> 404 (unknown AND cross-tenant ids)
- AutomationLifecycleConflictError -> 409 (deleting / deleted)
"""
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from src.api.deps import get_authenticated_entity, get_reload_publisher
from src.bl import lifecycle
from src.core.reload import ReloadPublisher
from src.models.api.automation import LifecycleStatusOut
from src.models.api.identity import AuthenticatedEntity

router = APIRouter(dependencies=[Depends(get_authenticated_entity)])


@router.post("/automations/{automation_id}/enable", status_code=200)
async def enable_automation(
    automation_id: UUID,
    entity: AuthenticatedEntity = Depends(get_authenticated_entity),
    publisher: ReloadPublisher = Depends(get_reload_publisher),
):
    automation = await run_in_threadpool(
        lifecycle.enable_automation,
        entity.tenant_id,
        automation_id,
        entity.email,
        publisher,
    )
    return LifecycleStatusOut.from_orm(automation).dict()


@router.post("/automations/{automation_id}/disable", status_code=200)
async def disable_automation(
    automation_id: UUID,
    entity: AuthenticatedEntity = Depends(get_authenticated_entity),
    publisher: ReloadPublisher = Depends(get_reload_publisher),
):
    automation = await run_in_threadpool(
        lifecycle.disable_automation,
        entity.tenant_id,
        automation_id,
        entity.email,
        publisher,
    )
    return LifecycleStatusOut.from_orm(automation).dict()
