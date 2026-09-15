"""Tier auth dependencies (D12 skeleton — placeholders, no real verification).

- user tier  -> the existing identity provider. Only a noauth shim is wired now;
  the shared identity manager is vendored later (never a second auth stack, §10.2).
- machine tier -> GitLab secret token (verified in D15).
- internal tier -> service token (verified in D17/D20).
"""
from fastapi import Depends, Header

from src.bl.cascade import CascadeDeps
from src.bl.cascade_adapters import (
    CappDeletionClient,
    RegistryClient,
    get_default_capp_deletion_client,
    get_default_registry_client,
)
from src.bl.git_client import GitClient, get_default_git_client
from src.core.reload import ReloadPublisher, get_default_reload_publisher
from src.models.api.identity import AuthenticatedEntity


def get_git_client() -> GitClient:
    """Script-repo client. In-memory stub until D14 lands the GitLab client."""
    return get_default_git_client()


def get_reload_publisher() -> ReloadPublisher:
    """Redis `reload` publisher; a no-op when REDIS_URL is unset."""
    return get_default_reload_publisher()


def get_capp_deletion_client() -> CappDeletionClient:
    """CAPP Capp/Secret deletion. Fails closed until D16's client is wired."""
    return get_default_capp_deletion_client()


def get_registry_client() -> RegistryClient:
    """Registry image deletion. Fails closed until A0's credentials are wired."""
    return get_default_registry_client()


def get_cascade_deps(
    capp: CappDeletionClient = Depends(get_capp_deletion_client),
    registry: RegistryClient = Depends(get_registry_client),
    git: GitClient = Depends(get_git_client),
    publisher: ReloadPublisher = Depends(get_reload_publisher),
) -> CascadeDeps:
    """The delete cascade's adapters, bundled. Each stays individually
    overridable (tests, and D14/D16/A0 swapping in real clients)."""
    return CascadeDeps(capp=capp, registry=registry, git=git, publisher=publisher)


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
