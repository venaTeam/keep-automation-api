"""Tier auth dependencies (D12 skeleton — placeholders, no real verification).

- user tier  -> the existing identity provider. Only a noauth shim is wired now;
  the shared identity manager is vendored later (never a second auth stack, §10.2).
- machine tier -> GitLab secret token (verified in D15).
- internal tier -> service token (verified in D17/D20).
"""
from fastapi import Header

from src.bl.git_client import GitClient, build_gitlab_client
from src.models.api.identity import AuthenticatedEntity


def get_git_client() -> GitClient:
    """Script-repo client — a fresh real GitLab client per request.

    Not a process-wide singleton: python-gitlab holds a non-thread-safe
    requests.Session and the BL runs in a threadpool, so a shared client could
    interleave concurrent requests' commits/reads. Construction does no network
    I/O (the project handle is lazy), so per-request creation is cheap.

    `build_gitlab_client` refuses under the noauth shim and fails fast if GitLab
    is unconfigured, so a misprovisioned pod surfaces on the first create/get
    rather than silently dropping commits. Tests inject the in-memory stub via
    dependency_overrides, so this GitLab path never runs under pytest.
    """
    return build_gitlab_client()


async def get_authenticated_entity() -> AuthenticatedEntity:
    """User-tier identity. Noauth shim returns a static entity for now.

    `tenant_id` here is the ONLY source of tenant scope for the user tier — the
    BL takes it as an explicit argument and no request body may supply it
    (spec §4.1/§8.1). When the shared identity manager replaces this shim it
    must keep returning an AuthenticatedEntity, which is what the routes and
    every tenant-scoped query downstream are typed against.
    """
    return AuthenticatedEntity(tenant_id="keep", email="noauth@keep")


async def verify_ci_webhook_token(x_gitlab_token: str | None = Header(default=None)):
    """Machine-tier gate. Real GitLab-token verification lands in D15."""
    return x_gitlab_token


async def verify_service_token(authorization: str | None = Header(default=None)):
    """Internal-tier gate. Real service-token verification lands in D17/D20."""
    return authorization
