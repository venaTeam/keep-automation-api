"""Async build-time deployment policy over the CAPP resource client.

Creation recovers conflicts in the requested wallet; rollout never upserts.
The coordinator waits for serving readiness before D15 commits a digest.
It owns no database connections or HTTP pool. F24 owns run-auth provisioning;
existing system mounts and unrelated environment entries are preserved.
"""

import asyncio
import math
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from src.bl.capp.client import CappClient
from src.bl.capp.errors import CappError, CappErrorCode
from src.bl.capp.models import (
    CappRequest,
    CappResource,
    ConditionSource,
    ConditionStatus,
)
from src.bl.cascade_adapters import capp_name_for
from src.exceptions import CappUnavailableError

_SECRETS_PATH = "/etc/automation/secrets"
_TEAM_VOLUME = "automation-secrets"
_MANAGED_ENV = frozenset({"AUTOMATION_ID", "AUTOMATION_SECRETS_PATH"})


@dataclass(frozen=True)
class CappDeployment:
    deployment_id: str
    deployment_url: str


class CappDeploymentClient(Protocol):
    async def create_deployment(
        self, *, automation_id: UUID, wallet: str, image: str, secret_refs: list[str]
    ) -> CappDeployment: ...

    async def rollout(
        self,
        *,
        automation_id: UUID,
        wallet: str,
        deployment_id: str,
        image: str,
        secret_refs: list[str],
    ) -> None: ...


class UnconfiguredCappClient:
    async def create_deployment(self, **_kwargs) -> CappDeployment:
        raise CappUnavailableError("CAPP deployment not configured")

    async def rollout(self, **_kwargs) -> None:
        raise CappUnavailableError("CAPP deployment not configured")


def _request(
    automation_id: UUID,
    name: str,
    image: str,
    secret_refs: list[str],
    current: CappResource | None = None,
) -> CappRequest:
    # The automation definition has one optional team Secret. Never read its
    # values or preflight its existence: CAPP reports missing mounts at runtime.
    if len(secret_refs) > 1 or any(not ref for ref in secret_refs):
        raise CappError(CappErrorCode.INVALID_REQUEST)
    previous = current.config if current else {}
    env = [
        entry
        for entry in previous.get("env", []) or []
        if entry["name"] not in _MANAGED_ENV
    ]
    env.extend(
        [
            {"name": "AUTOMATION_ID", "value": str(automation_id)},
            {"name": "AUTOMATION_SECRETS_PATH", "value": _SECRETS_PATH},
        ]
    )
    volumes = [
        entry
        for entry in previous.get("secretVolumes", []) or []
        if entry["name"] != _TEAM_VOLUME
    ]
    if secret_refs:
        volumes.append(
            {
                "name": _TEAM_VOLUME,
                "secretName": secret_refs[0],
                "mountPath": _SECRETS_PATH,
            }
        )
    config = {"state": "enabled", "env": env, "secretVolumes": volumes}
    if current is None:
        config.update(
            {
                "size": "small",
                "scaleSpec": {"minReplicas": 0},
                "routeSpec": {"tlsEnabled": True},
            }
        )
    return CappRequest(name=name, image=image, config=config)


class CappDeploymentCoordinator:
    def __init__(
        self,
        client: CappClient,
        *,
        readiness_timeout_seconds: float = 600,
        poll_interval_seconds: float = 1,
    ) -> None:
        try:
            valid = all(
                math.isfinite(value) and value > 0
                for value in (readiness_timeout_seconds, poll_interval_seconds)
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise CappError(CappErrorCode.CONFIGURATION)
        self._client = client
        self._timeout = readiness_timeout_seconds
        self._poll_interval = poll_interval_seconds

    async def create_deployment(
        self, *, automation_id: UUID, wallet: str, image: str, secret_refs: list[str]
    ) -> CappDeployment:
        name = capp_name_for(automation_id, None)
        try:
            async with asyncio.timeout(self._timeout):
                try:
                    resource = await self._client.create_capp(
                        wallet, _request(automation_id, name, image, secret_refs)
                    )
                except CappError as exc:
                    if exc.code != CappErrorCode.CONFLICT:
                        raise
                    resource = await self._reconcile(
                        automation_id, wallet, name, image, secret_refs
                    )
                ready = await self._wait_ready(wallet, name, resource.uid, image)
                return CappDeployment(ready.uid, ready.deployment_url)
        except (CappError, TimeoutError):
            pass
        # Outside the handler: raw errors/tracebacks must not be chained into
        # webhook exceptions, logs or HTTP responses. Cancellation propagates.
        raise CappUnavailableError("CAPP deployment failed or readiness timed out")

    async def rollout(
        self,
        *,
        automation_id: UUID,
        wallet: str,
        deployment_id: str,
        image: str,
        secret_refs: list[str],
    ) -> None:
        name = capp_name_for(automation_id, deployment_id)
        try:
            async with asyncio.timeout(self._timeout):
                resource = await self._reconcile(
                    automation_id,
                    wallet,
                    name,
                    image,
                    secret_refs,
                    expected_id=deployment_id,
                )
                await self._wait_ready(wallet, name, resource.uid, image)
                return
        except (CappError, TimeoutError):
            pass
        raise CappUnavailableError("CAPP rollout failed or readiness timed out")

    async def _reconcile(
        self,
        automation_id: UUID,
        wallet: str,
        name: str,
        image: str,
        secret_refs: list[str],
        expected_id: str | None = None,
    ) -> CappResource:
        current = await self._client.get_capp(wallet, name)
        if expected_id is not None and expected_id not in (current.uid, current.name):
            raise CappError(CappErrorCode.CONFLICT)
        spec = _request(automation_id, name, image, secret_refs, current)
        # Replaying an accepted update must not continuously start new revisions.
        if current.image == image and all(
            current.config.get(key) == value for key, value in spec.config.items()
        ):
            return current
        updated = await self._client.update_capp(wallet, name, spec)
        if updated.uid != current.uid:
            raise CappError(CappErrorCode.CONFLICT)
        return updated

    async def _wait_ready(
        self, wallet: str, name: str, uid: str, image: str
    ) -> CappResource:
        while True:
            resource = await self._client.get_capp(wallet, name)
            if resource.uid != uid:
                raise CappError(CappErrorCode.CONFLICT)
            ready = [
                condition
                for condition in resource.conditions
                if condition.type == "Ready"
            ]
            serving = any(
                condition.source
                in (None, ConditionSource.CAPP, ConditionSource.KNATIVE)
                for condition in ready
            )
            # A route's Ready=True alone does not prove the image is serving.
            # The API exposes no observedGeneration; match UID and requested
            # image, and require all exposed Ready conditions to be true.
            if (
                resource.image == image
                and resource.deployment_url
                and serving
                and all(condition.status == ConditionStatus.TRUE for condition in ready)
            ):
                return resource
            await asyncio.sleep(self._poll_interval)
