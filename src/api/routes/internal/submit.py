"""Internal-tier routes for the consumer + reconciler (stubs — logic lands in D17/D20).

Service-token auth + network-restricted (placeholder verification for now).

No table access yet. When D17 fills this in, the request carries `tenant_id`
(automation-contracts.md §submit) and the server must re-read the `automations`
row and **refuse** the submit if that row's `tenant_id` differs — an error
response, not a `suppressed` run, and `/run` is never called. `tenant_id` stays
out of the `(history_id, automation_id)` uniqueness key.
"""
from fastapi import APIRouter, Depends

from src.api.deps import verify_service_token

router = APIRouter(dependencies=[Depends(verify_service_token)])


@router.post("/internal/submit", status_code=200)
async def submit():
    return {"status": "ok"}
