"""Async CAPP resource operations for caller-supplied wallet targets.

Await calls after releasing DB sessions. Authentication and HTTP live in
CappTransport; response validation lives in capp.responses.
"""

import copy
import re
from collections.abc import Callable
from datetime import datetime
from http import HTTPMethod
from typing import TypeAlias

import httpx

from src.bl.capp.errors import CappError, CappErrorCode
from src.bl.capp.models import (
    JsonObject,
    CappRequest,
    CappResource,
    CappWalletTarget,
    SecretRequest,
    SecretResource,
    SecretUpdateRequest,
)
from src.bl.capp.responses import parse_capp, parse_secret
from src.bl.capp.transport import CappTransport
from src.bl.capp.validation import CONFIG_FIELDS, validate_config

WalletTarget: TypeAlias = CappWalletTarget | str

_PATH_SEGMENT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
_IMAGE_DIGEST = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
_OWNED_SECRET = re.compile(
    r"automation-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-run-auth"
)


def _segment(value: str) -> str:
    if not isinstance(value, str) or not _PATH_SEGMENT.fullmatch(value):
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
        cluster: str,
        timeout_seconds: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._cluster = _segment(cluster)
        self._transport = CappTransport(
            base_url=base_url,
            username=username,
            password=password,
            timeout_seconds=timeout_seconds,
            transport=transport,
            clock=clock,
        )

    def _target(self, wallet: WalletTarget) -> CappWalletTarget:
        if isinstance(wallet, CappWalletTarget):
            return CappWalletTarget(
                _segment(wallet.cluster), _segment(wallet.wallet_name)
            )
        return CappWalletTarget(self._cluster, _segment(wallet))

    @staticmethod
    def _path(target: CappWalletTarget, kind: str, name: str | None = None) -> str:
        """Build a resource path from a target validated by the public operation."""
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
        if not isinstance(spec.image, str) or not _IMAGE_DIGEST.fullmatch(spec.image):
            raise CappError(CappErrorCode.INVALID_REQUEST)
        if not isinstance(spec.config, dict) or spec.config.keys() - CONFIG_FIELDS:
            raise CappError(CappErrorCode.INVALID_REQUEST)
        if spec.config.get("state", "enabled") != "enabled":
            raise CappError(CappErrorCode.INVALID_REQUEST)
        validate_config(spec.config)
        return {**copy.deepcopy(spec.config), "name": spec.name, "image": spec.image}

    async def create_capp(
        self, wallet: WalletTarget, spec: CappRequest
    ) -> CappResource:
        target = self._target(wallet)
        path = self._path(target, "capps")
        raw = await self._transport.request(
            HTTPMethod.POST, path, self._capp_body(spec)
        )
        return parse_capp(raw, target.wallet_name, spec.name)

    async def get_capp(self, wallet: WalletTarget, name: str) -> CappResource:
        target = self._target(wallet)
        return parse_capp(
            await self._transport.request(
                HTTPMethod.GET, self._path(target, "capps", name)
            ),
            target.wallet_name,
            name,
        )

    async def update_capp(
        self, wallet: WalletTarget, name: str, spec: CappRequest
    ) -> CappResource:
        target = self._target(wallet)
        path = self._path(target, "capps", name)
        changes = self._capp_body(spec)
        if spec.name != name:
            raise CappError(CappErrorCode.INVALID_REQUEST)
        current = await self._transport.request(HTTPMethod.GET, path)
        parse_capp(current, target.wallet_name, name)
        body = {k: v for k, v in current.items() if k in CONFIG_FIELDS}
        # Response 'resources' is the resolved value; request uses customResources.
        if not body.get("size") and current.get("resources"):
            body["customResources"] = current["resources"]
        validate_config(body, error_code=CappErrorCode.MALFORMED_RESPONSE)
        body = _merge(body, changes)
        if changes.get("size") is not None:
            body.pop("customResources", None)
        elif changes.get("customResources") is not None:
            body.pop("size", None)
        validate_config({k: v for k, v in body.items() if k in CONFIG_FIELDS})
        # Per caap.yaml the backend preserves metadata/resourceVersion itself.
        return parse_capp(
            await self._transport.request(HTTPMethod.PUT, path, body),
            target.wallet_name,
            name,
        )

    async def delete_capp(self, wallet: WalletTarget, name: str) -> None:
        target = self._target(wallet)
        await self._transport.request(
            HTTPMethod.DELETE, self._path(target, "capps", name)
        )

    def _secret_path(
        self, target: CappWalletTarget, name: str, collection: bool = False
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
        self, wallet: WalletTarget, spec: SecretRequest
    ) -> SecretResource:
        target = self._target(wallet)
        path = self._secret_path(target, spec.name, collection=True)
        body = {
            "name": spec.name,
            "type": "Opaque",
            "data": self._secret_data(spec.data),
        }
        return parse_secret(
            await self._transport.request(HTTPMethod.POST, path, body),
            target.wallet_name,
            spec.name,
        )

    async def get_secret(self, wallet: WalletTarget, name: str) -> SecretResource:
        target = self._target(wallet)
        return parse_secret(
            await self._transport.request(
                HTTPMethod.GET, self._secret_path(target, name)
            ),
            target.wallet_name,
            name,
        )

    async def update_secret(
        self, wallet: WalletTarget, name: str, spec: SecretUpdateRequest
    ) -> SecretResource:
        target = self._target(wallet)
        path = self._secret_path(target, name)
        body = {"data": self._secret_data(spec.data)}
        return parse_secret(
            await self._transport.request(HTTPMethod.PUT, path, body),
            target.wallet_name,
            name,
        )

    async def delete_secret(self, wallet: WalletTarget, name: str) -> None:
        target = self._target(wallet)
        await self._transport.request(
            HTTPMethod.DELETE, self._secret_path(target, name)
        )
