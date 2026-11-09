"""Async deployment double for webhook orchestration tests."""

from uuid import UUID
from src.bl.capp.deployment import CappDeployment
from src.exceptions import CappUnavailableError


class InMemoryCappClient:
    """Test double honouring the contract above, recording every call."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.deployments: dict[str, CappDeployment] = {}
        self.wallets: dict[str, str] = {}
        self.images: dict[str, str] = {}
        self.calls: list[tuple] = []

    async def create_deployment(
        self,
        *,
        automation_id: UUID,
        wallet: str,
        image: str,
        secret_refs: list[str],
    ) -> CappDeployment:
        self.calls.append(
            ("create", str(automation_id), wallet, image, tuple(secret_refs))
        )
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

    async def rollout(
        self,
        *,
        automation_id: UUID,
        wallet: str,
        deployment_id: str,
        image: str,
        secret_refs: list[str],
    ) -> None:
        self.calls.append(("rollout", deployment_id, image, tuple(secret_refs)))
        if self.fail:
            raise CappUnavailableError("CAPP unavailable")
        self.images[deployment_id] = image
