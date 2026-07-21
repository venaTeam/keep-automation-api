"""Internal-tier routes for the consumer + reconciler (stubs — logic lands in D17/D20).

Service-token auth + network-restricted (placeholder verification for now).
"""
from fastapi import APIRouter, Depends

from src.api.deps import verify_service_token

router = APIRouter(dependencies=[Depends(verify_service_token)])


@router.post("/internal/submit")
async def submit():
    return {"status": "ok"}
