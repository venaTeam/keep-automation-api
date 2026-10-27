"""Synchronous CAPP adapter. Call off the event loop, after releasing DB sessions.

No readiness polling, database writes, transport retries, or deployment /run
calls live here. Authentication is the only automatic retry (once after 401).
"""

import copy
import json
import math
import re
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from http import HTTPMethod, HTTPStatus
from http.client import HTTPException, HTTPResponse
from typing import Any, Protocol, TypeAlias
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from src.bl.capp_errors import CappError, CappErrorCode, error_for_status
from src.bl.capp_models import (
    CappCondition,
    CappRequest,
    CappResource,
    CappWalletTarget,
    ConditionSource,
    ConditionStatus,
    SecretRequest,
    SecretResource,
    SecretUpdateRequest,
)

JsonObject: TypeAlias = dict[str, Any]
WalletTarget: TypeAlias = CappWalletTarget | str

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_TOKEN_REFRESH_SKEW_SECONDS = 30
_CONDITION_STATUSES = frozenset(ConditionStatus)
_CONDITION_SOURCES = frozenset(ConditionSource)
_SUCCESS_STATUS = {
    HTTPMethod.POST: HTTPStatus.CREATED,
    HTTPMethod.GET: HTTPStatus.OK,
    HTTPMethod.PUT: HTTPStatus.OK,
}


class HttpOpener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> HTTPResponse:
        """Open one HTTP request; implementations must honor its timeout."""
        ...


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


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    return value


def _optional_object(payload: JsonObject, key: str) -> JsonObject:
    value = payload.get(key)
    return {} if value is None else _object(value)


def _required_string(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    return value


def _optional_string(payload: JsonObject, key: str) -> str | None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    return value


def _conditions(payload: JsonObject) -> tuple[CappCondition, ...]:
    status = _optional_object(payload, "status")
    items = status.get("conditions", [])
    if not isinstance(items, list):
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    conditions = []
    for item in items:
        item = _object(item)
        condition_type = _required_string(item, "type")
        condition_status = _required_string(item, "status")
        source = _optional_string(item, "source")
        if condition_status not in _CONDITION_STATUSES:
            raise CappError(CappErrorCode.MALFORMED_RESPONSE)
        if source is not None and source not in _CONDITION_SOURCES:
            raise CappError(CappErrorCode.MALFORMED_RESPONSE)
        # Freeform reason/message can echo secrets. Deliberately omit them.
        conditions.append(
            CappCondition(
                type=condition_type,
                status=ConditionStatus(condition_status),
                source=ConditionSource(source) if source is not None else None,
            )
        )
    return tuple(conditions)


def _deployment_url(payload: JsonObject) -> str | None:
    route = _optional_object(payload, "routeSpec")
    hostname = _optional_string(route, "hostname")
    if not hostname:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", hostname):
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    tls_enabled = route.get("tlsEnabled", False)
    if not isinstance(tls_enabled, bool):
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    scheme = "https" if tls_enabled else "http"
    return f"{scheme}://{hostname}"


class CappClient:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        wallets: Mapping[str, str],
        timeout_seconds: float = 30,
        opener: HttpOpener | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        try:
            url = urlsplit(base_url)
            valid = (
                url.scheme == "https"
                and bool(url.hostname)
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
                and not any(c.isspace() or ord(c) < 32 for c in base_url)
            )
            url.port  # Validate malformed port syntax before any network call.
            valid = valid and math.isfinite(timeout_seconds) and timeout_seconds > 0
        except (ValueError, TypeError):
            valid = False
        if (
            not valid
            or not isinstance(username, str)
            or not username
            or not isinstance(password, str)
            or not password
        ):
            raise CappError(CappErrorCode.CONFIGURATION)
        if not isinstance(wallets, Mapping):
            raise CappError(CappErrorCode.CONFIGURATION)
        self._wallets = {_segment(w): _segment(c) for w, c in wallets.items()}
        self._base_url = base_url.rstrip("/")
        self._username, self._password = username, password
        self._timeout = timeout_seconds
        self._opener = opener
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._auth_lock = threading.Lock()
        self._access_token = self._refresh_token = ""
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)

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

    def _send(
        self,
        method: HTTPMethod,
        path: str,
        body: JsonObject | None = None,
        token: str = "",
    ) -> tuple[int, JsonObject]:
        # Request-local opener avoids shared cookies/transport state. Never follow
        # redirects, which could forward the service identity to another origin.
        error = CappError(CappErrorCode.MALFORMED_RESPONSE)
        try:
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = Request(
                self._base_url + path,
                method=method,
                headers=headers,
                data=json.dumps(body).encode() if body is not None else None,
            )
            open_request = self._opener or build_opener(_NoRedirect()).open
            with open_request(request, timeout=self._timeout) as response:
                status = response.status
                if (
                    status >= HTTPStatus.MULTIPLE_CHOICES
                    or status == HTTPStatus.NO_CONTENT
                ):
                    return status, {}
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    error = CappError(CappErrorCode.MALFORMED_RESPONSE)
                else:
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        return status, payload
                    error = CappError(CappErrorCode.MALFORMED_RESPONSE)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            return status, {}
        except TimeoutError:
            error = CappError(CappErrorCode.TIMEOUT, retryable=True)
        except URLError as exc:
            error = CappError(
                (
                    CappErrorCode.TIMEOUT
                    if isinstance(exc.reason, TimeoutError)
                    else CappErrorCode.UNAVAILABLE
                ),
                retryable=True,
            )
        except (OSError, HTTPException):
            error = CappError(CappErrorCode.UNAVAILABLE, retryable=True)
        except (ValueError, TypeError, UnicodeError):
            error = CappError(CappErrorCode.MALFORMED_RESPONSE)
        # Raise outside the handler: raw exceptions (potentially containing
        # passwords, tokens or Secret bodies) are not retained as __context__.
        raise error

    def _authenticate(self, failed_token: str | None = None) -> str:
        with self._auth_lock:
            usable = (
                self._access_token
                and (self._expires_at - self._clock()).total_seconds()
                > _TOKEN_REFRESH_SKEW_SECONDS
            )
            if usable and (failed_token is None or self._access_token != failed_token):
                return self._access_token
            self._access_token = ""
            if self._refresh_token:
                try:
                    self._login_request(
                        "refresh", {"refreshToken": self._refresh_token}
                    )
                    return self._access_token
                except CappError:
                    self._refresh_token = ""
            self._login_request(
                "login", {"username": self._username, "password": self._password}
            )
            return self._access_token

    def _login_request(self, operation: str, body: JsonObject) -> None:
        status, payload = self._send(HTTPMethod.POST, f"/api/v1/auth/{operation}", body)
        if status != HTTPStatus.OK:
            raise error_for_status(status)
        access = _required_string(payload, "accessToken")
        refresh = _required_string(payload, "refreshToken")
        expires_text = _required_string(payload, "expiresAt")
        expires = None
        try:
            expires = datetime.fromisoformat(expires_text.replace("Z", "+00:00"))
        except ValueError:
            pass
        if (
            expires is None
            or expires.tzinfo is None
            or expires <= self._clock()
            or not all(32 < ord(char) < 127 for char in access + refresh)
        ):
            raise CappError(CappErrorCode.MALFORMED_RESPONSE)
        self._access_token = access
        self._refresh_token = refresh
        self._expires_at = expires

    def _request(
        self, method: HTTPMethod, path: str, body: JsonObject | None = None
    ) -> JsonObject:
        token = self._authenticate()
        status, payload = self._send(method, path, body, token)
        if status == HTTPStatus.UNAUTHORIZED:
            token = self._authenticate(failed_token=token)
            status, payload = self._send(method, path, body, token)
        if method == HTTPMethod.DELETE and status in (
            HTTPStatus.NO_CONTENT,
            HTTPStatus.NOT_FOUND,
        ):
            return {}
        if status == _SUCCESS_STATUS.get(method):
            if self._contains_identity(payload):
                raise CappError(CappErrorCode.MALFORMED_RESPONSE)
            return payload
        raise error_for_status(status)

    def _contains_identity(self, value: object) -> bool:
        """Do not expose identity material echoed in an otherwise valid body."""
        if isinstance(value, str):
            return any(
                secret and secret in value
                for secret in (
                    self._username,
                    self._password,
                    self._access_token,
                    self._refresh_token,
                )
            )
        if isinstance(value, dict):
            return any(
                self._contains_identity(k) or self._contains_identity(v)
                for k, v in value.items()
            )
        if isinstance(value, list):
            return any(self._contains_identity(item) for item in value)
        return False

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

    def _capp_result(
        self,
        raw: JsonObject,
        target: WalletTarget,
        name: str,
    ) -> CappResource:
        namespace = self._target(target).wallet_name
        if raw.get("name") != name or raw.get("namespace", namespace) != namespace:
            raise CappError(CappErrorCode.MALFORMED_RESPONSE)
        return CappResource(
            name=name,
            uid=_required_string(raw, "uid"),
            deployment_url=_deployment_url(raw),
            image=_required_string(raw, "image"),
            conditions=_conditions(raw),
            resource_version=_optional_string(raw, "resourceVersion"),
        )

    def create_capp(self, target: WalletTarget, spec: CappRequest) -> CappResource:
        path = self._path(target, "capps")
        raw = self._request(HTTPMethod.POST, path, self._capp_body(spec))
        return self._capp_result(raw, target, spec.name)

    def get_capp(self, target: WalletTarget, name: str) -> CappResource:
        return self._capp_result(
            self._request(HTTPMethod.GET, self._path(target, "capps", name)),
            target,
            name,
        )

    def update_capp(
        self, target: WalletTarget, name: str, spec: CappRequest
    ) -> CappResource:
        path = self._path(target, "capps", name)
        changes = self._capp_body(spec)
        if spec.name != name:
            raise CappError(CappErrorCode.INVALID_REQUEST)
        current = self._request(HTTPMethod.GET, path)
        self._capp_result(current, target, name)
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
        return self._capp_result(
            self._request(HTTPMethod.PUT, path, body), target, name
        )

    def delete_capp(self, wallet: WalletTarget, name: str) -> None:
        self._request(HTTPMethod.DELETE, self._path(wallet, "capps", name))

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

    def _secret_result(
        self,
        raw: JsonObject,
        target: WalletTarget,
        name: str,
    ) -> SecretResource:
        namespace = self._target(target).wallet_name
        if raw.get("name") != name or raw.get("namespace") != namespace:
            raise CappError(CappErrorCode.MALFORMED_RESPONSE)
        # Never return Secret data or arbitrary labels/annotations to callers.
        return SecretResource(
            name=name,
            namespace=namespace,
            uid=_required_string(raw, "uid"),
            resource_version=_optional_string(raw, "resourceVersion"),
        )

    def create_secret(
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
        return self._secret_result(
            self._request(HTTPMethod.POST, path, body), target, spec.name
        )

    def get_secret(self, target: WalletTarget, name: str) -> SecretResource:
        return self._secret_result(
            self._request(HTTPMethod.GET, self._secret_path(target, name)), target, name
        )

    def update_secret(
        self, target: WalletTarget, name: str, spec: SecretUpdateRequest
    ) -> SecretResource:
        path = self._secret_path(target, name)
        body = {"data": self._secret_data(spec.data)}
        self.get_secret(target, name)
        return self._secret_result(
            self._request(HTTPMethod.PUT, path, body), target, name
        )

    def delete_secret(self, wallet: WalletTarget, name: str) -> None:
        self._request(HTTPMethod.DELETE, self._secret_path(wallet, name))
