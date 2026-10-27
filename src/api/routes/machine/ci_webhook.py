"""Machine-tier CI webhook route (D15, spec §8.2).

Called by the script repo's pipeline (F23) when one automation's build
finishes. Network-restricted and gated by the GitLab secret token — the most
security-sensitive endpoint of the build path, since it flips an automation to
runnable. Thin by rule: the cutover lives in `src/bl/builds.py`.

Responses: `200 {status: applied|ignored, reason?}` — `ignored` is a no-op
(replay / superseded SHA / deleting), never an error, so CI does not retry it;
`401` bad token; `400` malformed payload; `404` unknown automation; `503` CAPP
deploy/rollout failed, or the row was busy / could not be written after CAPP
accepted — the build stays `building`, safe to redeliver. Any 5xx: redeliver.
"""
from fastapi import APIRouter, Depends, status
from fastapi.concurrency import run_in_threadpool

from src.api.ci_webhook_guard import CI_WEBHOOK_PATH
from src.api.deps import (
    get_capp_client,
    get_capp_deletion_client,
    get_event_publisher,
    get_reload_publisher,
    verify_ci_webhook_token,
)
from src.bl import builds
from src.bl.capp_client import CappClient
from src.bl.cascade_adapters import CappDeletionClient
from src.core.automation_events import AutomationEventPublisher
from src.core.reload import ReloadPublisher
from src.models.api.ci_webhook import CiWebhookIn, CiWebhookOut

router = APIRouter(dependencies=[Depends(verify_ci_webhook_token)])


@router.post(
    # The guard matches this exact path; one constant keeps them together.
    CI_WEBHOOK_PATH,
    status_code=status.HTTP_200_OK,
    response_model=CiWebhookOut,
    response_model_exclude_none=True,
)
async def ci_webhook(
    body: CiWebhookIn,
    capp: CappClient = Depends(get_capp_client),
    reload: ReloadPublisher = Depends(get_reload_publisher),
    events: AutomationEventPublisher = Depends(get_event_publisher),
    deletion: CappDeletionClient = Depends(get_capp_deletion_client),
) -> CiWebhookOut:
    return await run_in_threadpool(
        builds.apply_build_result, body, capp, reload, events, deletion
    )
