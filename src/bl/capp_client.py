"""Async CAPP resource operations with server-controlled wallet validation.

Await calls after releasing DB sessions. Authentication and HTTP live in
CappTransport; response validation lives in capp_responses.
"""

import copy
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from http import HTTPMethod
from typing import Any, TypeAlias

import httpx

from src.bl.capp_errors import CappError, CappErrorCode
from src.bl.capp_models import (
    CappRequest,
    CappResource,
    CappWalletTarget,
    SecretRequest,
    SecretResource,
    SecretUpdateRequest,
)
from src.bl.capp_responses import parse_capp, parse_secret
from src.bl.capp_transport import CappTransport

JsonObject: TypeAlias = dict[str, Any]
WalletTarget: TypeAlias = CappWalletTarget | str

_FIELDS = frozenset(
    (
        "scaleSpec",
        "state",
        "containerName",
        "size",
        "customResources",
        "env",
        "volumeMounts",
        "routeSpec",
        "logSpec",
        "nfsVolumes",
        "secretVolumes",
        "configMapVolumes",
        "eventSourcesSpec",
    )
)
_OWNED_SECRET = re.compile(
    r"automation-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-run-auth"
)


def _segment(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value
    ):
        raise CappError(CappErrorCode.INVALID_TARGET)
    return value


def _merge(current: JsonObject, changes: JsonObject) -> JsonObject:
    result = copy.deepcopy(current)
    for key, value in changes.items():
        result[key] = (
            _merge(result[key], value)
            if isinstance(result.get(key), dict) and isinstance(value, dict)
            else copy.deepcopy(value)
        )
    return result


class CappClient:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        wallets: Mapping[str, str],
        timeout_seconds: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(wallets, Mapping):
            raise CappError(CappErrorCode.CONFIGURATION)
        # Snapshot the onboarded wallet-to-cluster mapping, never caller input.
        self._wallets = {_segment(w): _segment(c) for w, c in wallets.items()}
        self._transport = CappTransport(
            base_url=base_url,
            username=username,
            password=password,
            timeout_seconds=timeout_seconds,
            transport=transport,
            clock=clock,
        )

    def _target(self, target: WalletTarget) -> CappWalletTarget:
        wallet = target.wallet_name if isinstance(target, CappWalletTarget) else target
        _segment(wallet)
        cluster = self._wallets.get(wallet)
        if not cluster or (
            isinstance(target, CappWalletTarget) and target.cluster != cluster
        ):
            raise CappError(CappErrorCode.INVALID_TARGET)
        return CappWalletTarget(cluster, wallet)

    def _path(self, target: WalletTarget, kind: str, name: str | None = None) -> str:
        target = self._target(target)
        suffix = f"/{_segment(name)}" if name is not None else ""
        return f"/api/v1/clusters/{target.cluster}/namespaces/{target.wallet_name}/{kind}{suffix}"

    async def __aenter__(self) -> "CappClient":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the connection pool when its owning application shuts down."""
        await self._transport.aclose()

    def _capp_body(self, spec: CappRequest) -> JsonObject:
        _segment(spec.name)
        if not isinstance(spec.image, str) or not re.fullmatch(
            r"[^\s@]+@sha256:[0-9a-f]{64}", spec.image
        ):
            raise CappError(CappErrorCode.INVALID_REQUEST)
        if not isinstance(spec.config, dict) or spec.config.keys() - _FIELDS:
            raise CappError(CappErrorCode.INVALID_REQUEST)
        if spec.config.get("size") and spec.config.get("customResources"):
            raise CappError(CappErrorCode.INVALID_REQUEST)
        if spec.config.get("state", "enabled") != "enabled":
            raise CappError(CappErrorCode.INVALID_REQUEST)
        return {**copy.deepcopy(spec.config), "name": spec.name, "image": spec.image}

    async def create_capp(
        self, target: WalletTarget, spec: CappRequest
    ) -> CappResource:
        path = self._path(target, "capps")
        raw = await self._transport.request(
            HTTPMethod.POST, path, self._capp_body(spec)
        )
        return parse_capp(raw, self._target(target).wallet_name, spec.name)

    async def get_capp(self, target: WalletTarget, name: str) -> CappResource:
        return parse_capp(
            await self._transport.request(
                HTTPMethod.GET, self._path(target, "capps", name)
            ),
            self._target(target).wallet_name,
            name,
        )

    async def update_capp(
        self, target: WalletTarget, name: str, spec: CappRequest
    ) -> CappResource:
        path = self._path(target, "capps", name)
        changes = self._capp_body(spec)
        if spec.name != name:
            raise CappError(CappErrorCode.INVALID_REQUEST)
        current = await self._transport.request(HTTPMethod.GET, path)
        parse_capp(current, self._target(target).wallet_name, name)
        body = {k: v for k, v in current.items() if k in _FIELDS}
        # Response 'resources' is the resolved value; request uses customResources.
        if not body.get("size") and current.get("resources"):
            body["customResources"] = current["resources"]
        body = _merge(body, changes)
        if changes.get("size"):
            body.pop("customResources", None)
        elif changes.get("customResources"):
            body.pop("size", None)
        # Per caap.yaml the backend preserves metadata/resourceVersion itself.
        return parse_capp(
            await self._transport.request(HTTPMethod.PUT, path, body),
            self._target(target).wallet_name,
            name,
        )

    async def delete_capp(self, wallet: WalletTarget, name: str) -> None:
        await self._transport.request(
            HTTPMethod.DELETE, self._path(wallet, "capps", name)
        )

    def _secret_path(
        self, target: WalletTarget, name: str, collection: bool = False
    ) -> str:
        if not isinstance(name, str) or not _OWNED_SECRET.fullmatch(name):
            raise CappError(CappErrorCode.INVALID_TARGET)
        return self._path(target, "secrets", None if collection else name)

    @staticmethod
    def _secret_data(data: dict[str, str]) -> dict[str, str]:
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise CappError(CappErrorCode.INVALID_REQUEST)
        return dict(data)

    async def create_secret(
        self, target: WalletTarget, spec: SecretRequest
    ) -> SecretResource:
        path = self._secret_path(target, spec.name, collection=True)
        if spec.type != "Opaque":
            raise CappError(CappErrorCode.INVALID_REQUEST)
        body = {
            "name": spec.name,
            "type": spec.type,
            "data": self._secret_data(spec.data),
        }
        return parse_secret(
            await self._transport.request(HTTPMethod.POST, path, body),
            self._target(target).wallet_name,
            spec.name,
        )

    async def get_secret(self, target: WalletTarget, name: str) -> SecretResource:
        return parse_secret(
            await self._transport.request(
                HTTPMethod.GET, self._secret_path(target, name)
            ),
            self._target(target).wallet_name,
            name,
        )

    async def update_secret(
        self, target: WalletTarget, name: str, spec: SecretUpdateRequest
    ) -> SecretResource:
        path = self._secret_path(target, name)
        body = {"data": self._secret_data(spec.data)}
        await self.get_secret(target, name)
        return parse_secret(
            await self._transport.request(HTTPMethod.PUT, path, body),
            self._target(target).wallet_name,
            name,
        )

    async def delete_secret(self, wallet: WalletTarget, name: str) -> None:
        await self._transport.request(
            HTTPMethod.DELETE, self._secret_path(wallet, name)
        )
