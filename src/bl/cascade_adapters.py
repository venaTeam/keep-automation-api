"""External interfaces used by D18's deletion cascade.

D16's general CAPP client implements the narrow deletion interface here.
D18 owns the Artifactory implementation; A0 supplies its deployment settings.
Unconfigured clients raise so cleanup never reports false completion.
CAPP operations are awaited; registry operations run in worker threads.
Neither holds a DB session across external calls.
"""

import threading
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from src import config
from src.bl.registry_client import ArtifactoryRegistryClient

if TYPE_CHECKING:
    from src.bl.capp_client import CappClient


class ExternalDependencyNotConfiguredError(RuntimeError):
    """A cascade adapter was called before its real implementation was wired."""


def capp_name_for(automation_id: UUID, capp_deployment_id: str | None) -> str:
    """CAPP paths take names, not UIDs returned by create (D16).

    Retain compatibility with rows that stored the name before D16. A UID
    needs the deterministic name used by the deployment coordinator.
    """
    if capp_deployment_id:
        try:
            UUID(capp_deployment_id)
        except ValueError:
            return capp_deployment_id
        return f"automation-{automation_id}"
    return capp_deployment_id or f"automation-{automation_id}"


def run_auth_secret_name(automation_id: UUID) -> str:
    """Exact reserved name of the Keep-owned Secret; never the team's Secret."""
    return f"automation-{automation_id}-run-auth"


@runtime_checkable
class CappDeletionClient(Protocol):
    async def delete_capp(self, wallet: str, name: str) -> None:
        """Delete the Capp. Return on 204/404; raise on other failures."""
        ...

    async def delete_secret(self, wallet: str, name: str) -> None:
        """Delete the Secret. Return on 204/404; raise on other failures."""
        ...


@runtime_checkable
class RegistryClient(Protocol):
    def list_image_entries(self, automation_id: UUID) -> list[str]:
        """Opaque cleanup references for all owned versions/build artifacts.

        The implementation scopes these to the automation's image path,
        excluding the shared golden base and every other automation.
        """
        ...

    def delete_image(self, automation_id: UUID, entry: str) -> None:
        """Delete an owned entry from the inventory. Already absent = return."""
        ...


class UnconfiguredCappDeletionClient:
    async def delete_capp(self, wallet: str, name: str) -> None:
        raise ExternalDependencyNotConfiguredError("CAPP client not configured (D16)")

    async def delete_secret(self, wallet: str, name: str) -> None:
        raise ExternalDependencyNotConfiguredError("CAPP client not configured (D16)")


class UnconfiguredRegistryClient:
    def list_image_entries(self, automation_id: UUID) -> list[str]:
        raise ExternalDependencyNotConfiguredError("Artifactory settings missing (A0)")

    def delete_image(self, automation_id: UUID, entry: str) -> None:
        raise ExternalDependencyNotConfiguredError("Artifactory settings missing (A0)")


_default_registry: RegistryClient | None = None
_registry_lock = threading.Lock()


def build_capp_client() -> "CappClient":
    """Construct a client for one application's event loop; its owner closes it."""
    from src.bl.capp_client import CappClient

    return CappClient(**config.load_capp_client_settings())


def get_default_registry_client() -> RegistryClient:
    global _default_registry
    with _registry_lock:
        if _default_registry is None:
            if config.ARTIFACTORY_URL:
                _default_registry = ArtifactoryRegistryClient(
                    base_url=config.ARTIFACTORY_URL,
                    repository=config.ARTIFACTORY_REPOSITORY,
                    image_prefix=config.ARTIFACTORY_IMAGE_PREFIX,
                    token=config.ARTIFACTORY_TOKEN,
                    username=config.ARTIFACTORY_USERNAME,
                    password=config.ARTIFACTORY_PASSWORD,
                    timeout_seconds=config.ARTIFACTORY_TIMEOUT_SECONDS,
                )
            else:
                _default_registry = UnconfiguredRegistryClient()
        return _default_registry
