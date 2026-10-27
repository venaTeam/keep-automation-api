"""CAPP control-API seam — deploy and roll out per-automation deployments.

CAPP is the execution plane (spec §3.3, §10.1): one long-running deployment per
automation, created on its first successful build and **rolled out** to the new
image on every later cutover (spec §5.1). Calls go out as our single CAPP
service identity, which never leaves this service (spec §3.4).

D15 ships the interface plus an in-memory stand-in — the same seam D13 used for
git before D14: D16 lands the real client behind `get_capp_client` without
touching callers. Until then production **fails closed**: the default client
raises `CappUnavailableError`, so a CI webhook answers 503 and leaves the build
lock in place instead of advancing `active_digest` to an image nothing serves.

Contract the real client must honour (automation-contracts.md §"CAPP
integration"):

- The deployment is named **`automation-{automation_id}`** and that name is
  what `capp_deployment_id` stores — the delete cascade (`capp_name_for`)
  falls back to it for a first deploy whose id was never stored.
- `create_deployment` is keyed by `automation_id` and **idempotent**: if a
  deployment for that id already exists it returns it rather than creating a
  second one. The cutover commits *after* the CAPP call (no pooled connection
  is held across network I/O — ADR-008), so a crash or DB failure between the
  two leaves a deployment whose id we never stored; the retried webhook must
  find it again, not orphan it.
- `rollout` leaves the previous revision serving when it fails (spec §5.1).
  It carries the image **and the current `secret_refs`** — an edit may change
  the secret, and the deployment must mount what the row names.
- The **wallet is fixed at create**: authoring refuses a wallet change once
  `capp_deployment_id` is set (`namespace_immutable`), so rollout never needs
  one. If `create_deployment` finds an existing deployment for the id in a
  *different* wallet (reachable only via the drift below: a create whose id
  was never stored, then a wallet edit before the next build), it must not
  return it as-is — tear it down and recreate in the requested wallet, or
  raise `CappUnavailableError`.
- CAPP can end up **ahead of the row**: if the cutover's commit-time admission
  fails after a successful call (deletion started, or E21 released the lock,
  mid-call), a rollout keeps serving the new image and a first-build create
  leaves a deployment whose id was never stored. So anything that tears down
  or reconciles a deployment (D18's delete cascade, E21) must resolve it by
  `automation_id` — the deployment key — not only by `capp_deployment_id`.
- Every call carries an explicit timeout and raises `CappUnavailableError` on
  any failure, with a message that never includes the identity.

Open for D16: `CappUnavailableError` means "retry" (503, lock kept). A
*permanent* rejection (e.g. an edit naming a secret the wallet lacks, if CAPP
checks refs at rollout) handled that way keeps the row `building` — and PUT
409ing — until E21. D16 should add a distinct rejection outcome that records
`build_failed` and releases the lock (automation-contracts.md §CAPP
integration).
"""
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from src.exceptions import CappUnavailableError


@dataclass(frozen=True)
class CappDeployment:
    # Stored as `automations.capp_deployment_id` / `deployment_url` (§4.4).
    deployment_id: str
    # The `/run` base URL — the hot-path invocation target (D17).
    deployment_url: str


class CappClient(Protocol):
    def create_deployment(
        self,
        *,
        automation_id: UUID,
        wallet: str,
        image: str,
        secret_refs: list[str],
    ) -> CappDeployment:
        """First successful build: create (or find) the deployment."""
        ...

    def rollout(
        self, *, deployment_id: str, image: str, secret_refs: list[str]
    ) -> None:
        """Later builds: roll the existing deployment out to `image`, mounting
        `secret_refs` (the wallet never changes — see the contract above)."""
        ...


class UnconfiguredCappClient:
    """Production default until D16: refuses every call (fail closed)."""

    def create_deployment(self, **_kwargs) -> CappDeployment:
        raise CappUnavailableError("CAPP client not configured (D16)")

    def rollout(self, **_kwargs) -> None:
        raise CappUnavailableError("CAPP client not configured (D16)")


class InMemoryCappClient:
    """Test double honouring the contract above, recording every call."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.deployments: dict[str, CappDeployment] = {}
        self.wallets: dict[str, str] = {}
        self.images: dict[str, str] = {}
        self.calls: list[tuple] = []

    def create_deployment(
        self,
        *,
        automation_id: UUID,
        wallet: str,
        image: str,
        secret_refs: list[str],
    ) -> CappDeployment:
        self.calls.append(("create", str(automation_id), wallet, image, tuple(secret_refs)))
        if self.fail:
            raise CappUnavailableError("CAPP unavailable")
        key = str(automation_id)
        deployment = self.deployments.get(key)
        if deployment is not None and self.wallets[key] != wallet:
            # An unstored create left a deployment in the old wallet: never
            # hand it back — tear it down and recreate in the requested one.
            self.calls.append(("teardown", deployment.deployment_id))
            self.images.pop(deployment.deployment_id, None)
            deployment = CappDeployment(
                deployment_id=f"automation-{key}",
                deployment_url=f"https://{wallet}.capp.test/{key}",
            )
            self.deployments[key] = deployment
            self.wallets[key] = wallet
        if deployment is None:
            deployment = CappDeployment(
                deployment_id=f"automation-{key}",
                deployment_url=f"https://{wallet}.capp.test/{key}",
            )
            self.deployments[key] = deployment
            self.wallets[key] = wallet
        self.images[deployment.deployment_id] = image
        return deployment

    def rollout(
        self, *, deployment_id: str, image: str, secret_refs: list[str]
    ) -> None:
        self.calls.append(("rollout", deployment_id, image, tuple(secret_refs)))
        if self.fail:
            raise CappUnavailableError("CAPP unavailable")
        self.images[deployment_id] = image


def get_default_capp_client() -> CappClient:
    return UnconfiguredCappClient()
