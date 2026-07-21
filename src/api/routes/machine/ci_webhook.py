"""Machine-tier CI webhook route (stub — logic lands in D15).

Network-restricted; verifies a GitLab secret token (placeholder for now).
"""
from fastapi import APIRouter, Depends

from src.api.deps import verify_ci_webhook_token

router = APIRouter()


@router.post("/internal/ci-webhook")
async def ci_webhook(_token: str | None = Depends(verify_ci_webhook_token)):
    return {"status": "accepted"}
