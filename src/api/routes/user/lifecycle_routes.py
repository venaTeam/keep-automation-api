"""User-tier lifecycle routes (D18, spec §8.1): enable / disable / DELETE.

Thin layer, same shape as `automations.py`: the tenant and actor come from the
session, the sync BL runs in the threadpool, and domain exceptions are mapped
once in `src/main.py`:

- AutomationValidationError        -> 400 (`active_digest_required`)
- AutomationNotFoundError          -> 404 (unknown AND cross-tenant ids)
- AutomationLifecycleConflictError -> 409 (deleting / deleted)
- AutomationBuildingError          -> 409 (DELETE while building)

These are the only three lifecycle routes D18 mounts. `resume-delete` is D20's
internal route over `cascade.resume_delete`; deboard has no route at all.
"""
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
from fastapi.concurrency import run_in_threadpool

from src.api.deps import (
    get_authenticated_entity,
    get_cascade_deps,
    get_reload_publisher,
)
from src.bl import cascade, lifecycle
from src.core.reload import ReloadPublisher
from src.models.api.automation import LifecycleStatusOut
from src.models.api.identity import AuthenticatedEntity
from src.models.db.automation import MatchingState

router = APIRouter(dependencies=[Depends(get_authenticated_entity)])


@router.post("/automations/{automation_id}/enable", status_code=status.HTTP_200_OK)
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


@router.post("/automations/{automation_id}/disable", status_code=status.HTTP_200_OK)
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


@router.delete(
    "/automations/{automation_id}",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_200_OK: {"description": "Already deleted"},
        status.HTTP_409_CONFLICT: {"description": "Automation is mid-build"},
    },
)
async def delete_automation(
    automation_id: UUID,
    response: Response,
    background_tasks: BackgroundTasks,
    entity: AuthenticatedEntity = Depends(get_authenticated_entity),
    deps: cascade.CascadeDeps = Depends(get_cascade_deps),
):
    """Start (or continue) the delete cascade; returns before any external call.

    The DELETE that admits the deletion (`202`) starts the one background
    attempt. A repeated DELETE only reports progress (`202` while deleting,
    `200` once deleted) and starts nothing — a stopped cascade is resumed by
    the reconciler (E21 → D20 → `cascade.resume_delete`), not by the UI.
    """
    admission = await run_in_threadpool(
        cascade.begin_delete,
        entity.tenant_id,
        automation_id,
        entity.email,
        deps.publisher,
    )
    automation = admission.automation
    if admission.started:
        background_tasks.add_task(
            cascade.run_cascade_in_background, automation.id, deps
        )
    elif automation.matching_state == MatchingState.DELETED:
        response.status_code = status.HTTP_200_OK
    return LifecycleStatusOut.from_orm(automation).dict()
