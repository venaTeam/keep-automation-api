"""Tier auth dependencies (D12 skeleton — placeholders, no real verification).

- user tier  -> the existing identity provider. Only a noauth shim is wired now;
  the shared identity manager is vendored later (never a second auth stack, §10.2).
- machine tier -> GitLab secret token (verified in D15).
- internal tier -> service token (verified in D17/D20).
"""
from fastapi import Header


async def get_authenticated_entity() -> dict:
    """User-tier identity. Noauth shim returns a static entity for now."""
    return {"tenant_id": "keep", "email": "noauth@keep"}


async def verify_ci_webhook_token(x_gitlab_token: str | None = Header(default=None)):
    """Machine-tier gate. Real GitLab-token verification lands in D15."""
    return x_gitlab_token


async def verify_service_token(authorization: str | None = Header(default=None)):
    """Internal-tier gate. Real service-token verification lands in D17/D20."""
    return authorization
