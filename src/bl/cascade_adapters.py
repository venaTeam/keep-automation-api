"""External seams used by the delete cascade (D18): CAPP deletion + registry.

The real implementations belong to other stories — the CAPP client to D16
(C5/C6 in D18-lifecycle-plan.md: it owns cluster resolution and auth refresh),
registry access to A0's credentials. D18 depends only on these Protocols, so
the cascade is fully testable before those land.

**Unconfigured defaults fail, they do not pretend.** Until a real adapter is
wired, the default raises. The cascade then stops at its last checkpoint and
the automation stays `deleting` — a stub that returned success would mark an
automation `deleted` while its Capp, run-auth Secret and images are still live,
which is exactly the false completion D18 forbids.

Contract for every implementation:

- Blocking I/O with an **explicit timeout** on every network call; called from
  the threadpool, never while a DB session is open.
- `204` and `404` are both success (delete-if-exists). `401`/`403`/`5xx`/timeout
  raise — never swallowed as "already gone".
- Exceptions must not carry credentials in their message (the cascade logs only
  the exception type, but adapters should not rely on that).
"""
from typing import Protocol, runtime_checkable
from uuid import UUID


class ExternalDependencyNotConfiguredError(RuntimeError):
    """A cascade adapter was called before its real implementation was wired."""


def capp_name_for(automation_id: UUID, capp_deployment_id: str | None) -> str:
    """The Capp to delete: the stored id, else the deterministic deploy name.

    The fallback matters: a first deploy can create resources and fail before
    `capp_deployment_id` is persisted, so a null id is not proof nothing exists.
    """
    return capp_deployment_id or f"automation-{automation_id}"


def run_auth_secret_name(automation_id: UUID) -> str:
    """The Keep-owned run-auth Secret for this automation — and only this one.

    Ownership is established by this exact reserved name: CAPP Secrets carry no
    labels we can set and team Secrets share CAPP's managed label, so the cascade
    never deletes by listing, pattern, or `secret_name` (the team's Secret).
    """
    return f"automation-{automation_id}-run-auth"


@runtime_checkable
class CappDeletionClient(Protocol):
    def delete_capp(self, wallet: str, name: str) -> None:
        """DELETE …/namespaces/{wallet}/capps/{name}. 204/404 = return."""
        ...

    def delete_secret(self, wallet: str, name: str) -> None:
        """DELETE …/namespaces/{wallet}/secrets/{name}. 204/404 = return."""
        ...


@runtime_checkable
class RegistryClient(Protocol):
    def list_image_digests(self, automation_id: UUID) -> list[str]:
        """Every image version under this automation's own registry path.

        Includes old/inactive digests and build artifacts, never the shared
        golden base or another automation's path.
        """
        ...

    def delete_image(self, automation_id: UUID, digest: str) -> None:
        """Delete one digest from this automation's path. Absent = return."""
        ...


class UnconfiguredCappDeletionClient:
    def delete_capp(self, wallet: str, name: str) -> None:
        raise ExternalDependencyNotConfiguredError("CAPP client not configured (D16)")

    def delete_secret(self, wallet: str, name: str) -> None:
        raise ExternalDependencyNotConfiguredError("CAPP client not configured (D16)")


class UnconfiguredRegistryClient:
    def list_image_digests(self, automation_id: UUID) -> list[str]:
        raise ExternalDependencyNotConfiguredError("registry client not configured (A0)")

    def delete_image(self, automation_id: UUID, digest: str) -> None:
        raise ExternalDependencyNotConfiguredError("registry client not configured (A0)")


_default_capp = UnconfiguredCappDeletionClient()
_default_registry = UnconfiguredRegistryClient()


def get_default_capp_deletion_client() -> CappDeletionClient:
    return _default_capp


def get_default_registry_client() -> RegistryClient:
    return _default_registry
