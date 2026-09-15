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

from fastapi import APIRouter, BackgroundTasks, Depends, Response
from fastapi.concurrency import run_in_threadpool

from src.api.deps import (
    get_authenticated_entity,
    get_capp_deletion_client,
    get_git_client,
    get_registry_client,
    get_reload_publisher,
)
from src.bl import cascade, lifecycle
from src.bl.cascade_adapters import CappDeletionClient, RegistryClient
from src.bl.git_client import GitClient
from src.core.reload import ReloadPublisher
from src.models.api.automation import LifecycleStatusOut
from src.models.api.identity import AuthenticatedEntity
from src.models.db.automation import MatchingState

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


@router.delete("/automations/{automation_id}", status_code=202)
async def delete_automation(
    automation_id: UUID,
    response: Response,
    background_tasks: BackgroundTasks,
    entity: AuthenticatedEntity = Depends(get_authenticated_entity),
    publisher: ReloadPublisher = Depends(get_reload_publisher),
    capp: CappDeletionClient = Depends(get_capp_deletion_client),
    registry: RegistryClient = Depends(get_registry_client),
    git: GitClient = Depends(get_git_client),
):
    """Start (or continue) the delete cascade; returns before any external call.

    `202` + progress while deleting — a repeated DELETE reports progress and
    launches another attempt, which is safe (compare-and-set checkpoints).
    `200` once already deleted.
    """
    automation = await run_in_threadpool(
        cascade.begin_delete,
        entity.tenant_id,
        automation_id,
        entity.email,
        publisher,
    )
    if automation.matching_state == MatchingState.DELETED:
        response.status_code = 200
    else:
        background_tasks.add_task(
            cascade.run_cascade_in_background,
            automation.id,
            cascade.CascadeDeps(
                capp=capp, registry=registry, git=git, publisher=publisher
            ),
        )
    return LifecycleStatusOut.from_orm(automation).dict()
