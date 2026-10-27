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
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from src.bl.capp_errors import CappAuthenticationError, CappConflictError, CappError
from src.bl.capp_models import (
    CappCondition, CappRequest, CappResource, CappWalletTarget,
    SecretRequest, SecretResource, SecretUpdateRequest,
)

_FIELDS = frozenset((
    "scaleSpec", "state", "containerName", "size", "customResources", "env",
    "volumeMounts", "routeSpec", "logSpec", "nfsVolumes", "secretVolumes",
    "configMapVolumes", "eventSourcesSpec",
))
_OWNED_SECRET = re.compile(
    r"automation-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-run-auth"
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _segment(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value):
        raise CappError("invalid_target")
    return value


def _merge(current: dict, changes: dict) -> dict:
    result = copy.deepcopy(current)
    for key, value in changes.items():
        result[key] = (_merge(result[key], value)
                       if isinstance(result.get(key), dict) and isinstance(value, dict)
                       else copy.deepcopy(value))
    return result


class CappClient:
    def __init__(self, *, base_url: str, username: str, password: str,
                 wallets: Mapping[str, str], timeout_seconds: float = 30,
                 opener: Callable | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        try:
            url = urlsplit(base_url)
            valid = (url.scheme == "https" and bool(url.hostname)
                     and not url.username and not url.password
                     and not url.query and not url.fragment
                     and not any(c.isspace() or ord(c) < 32 for c in base_url))
            valid = valid and math.isfinite(timeout_seconds) and timeout_seconds > 0
        except (ValueError, TypeError):
            valid = False
        if not valid or not username or not password:
            raise CappError("configuration")
        if not isinstance(wallets, Mapping):
            raise CappError("configuration")
        self._wallets = {_segment(w): _segment(c) for w, c in wallets.items()}
        self._base_url = base_url.rstrip("/")
        self._username, self._password = username, password
        self._timeout = timeout_seconds
        self._opener = opener
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._auth_lock = threading.Lock()
        self._access_token = self._refresh_token = ""
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)

    def _target(self, target: CappWalletTarget | str) -> CappWalletTarget:
        wallet = target.wallet_name if isinstance(target, CappWalletTarget) else target
        _segment(wallet)
        cluster = self._wallets.get(wallet)
        if not cluster or (isinstance(target, CappWalletTarget) and target.cluster != cluster):
            raise CappError("invalid_target")
        return CappWalletTarget(cluster, wallet)

    def _path(self, target, kind: str, name: str | None = None) -> str:
        target = self._target(target)
        suffix = f"/{_segment(name)}" if name is not None else ""
        return f"/api/v1/clusters/{target.cluster}/namespaces/{target.wallet_name}/{kind}{suffix}"

    def _send(self, method: str, path: str, body=None, token: str = "") -> tuple[int, dict]:
        # Request-local opener avoids shared cookies/transport state. Never follow
        # redirects, which could forward the service identity to another origin.
        error = None
        try:
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = Request(self._base_url + path, method=method, headers=headers,
                              data=json.dumps(body).encode() if body is not None else None)
            open_request = self._opener or build_opener(_NoRedirect()).open
            with open_request(request, timeout=self._timeout) as response:
                status = response.status
                if status >= 300 or status == 204:
                    return status, {}
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    error = CappError("malformed_response")
                else:
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        return status, payload
                    error = CappError("malformed_response")
        except HTTPError as exc:
            status = exc.code
            exc.close()
            return status, {}
        except TimeoutError:
            error = CappError("timeout", retryable=True)
        except URLError as exc:
            error = CappError("timeout" if isinstance(exc.reason, TimeoutError) else "unavailable",
                              retryable=True)
        except (OSError, HTTPException):
            error = CappError("unavailable", retryable=True)
        except (ValueError, TypeError, UnicodeError):
            error = CappError("malformed_response")
        # Raise outside the handler: raw exceptions (potentially containing
        # passwords, tokens or Secret bodies) are not retained as __context__.
        raise error

    def _authenticate(self, failed_token: str | None = None) -> str:
        with self._auth_lock:
            usable = (self._access_token
                      and (self._expires_at - self._clock()).total_seconds() > 30)
            if usable and (failed_token is None or self._access_token != failed_token):
                return self._access_token
            self._access_token = ""
            if self._refresh_token:
                try:
                    self._login_request("refresh", {"refreshToken": self._refresh_token})
                    return self._access_token
                except CappError:
                    self._refresh_token = ""
            self._login_request("login", {"username": self._username, "password": self._password})
            return self._access_token

    def _login_request(self, operation: str, body: dict) -> None:
        status, payload = self._send("POST", f"/api/v1/auth/{operation}", body)
        if status != 200:
            raise CappAuthenticationError(status)
        valid = False
        try:
            access, refresh = payload["accessToken"], payload["refreshToken"]
            expires = datetime.fromisoformat(payload["expiresAt"].replace("Z", "+00:00"))
            valid = (isinstance(access, str) and bool(access)
                     and isinstance(refresh, str) and bool(refresh)
                     and all(32 < ord(c) < 127 for c in access + refresh)
                     and expires.tzinfo is not None and expires > self._clock())
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        if not valid:
            raise CappError("malformed_response")
        self._access_token, self._refresh_token, self._expires_at = access, refresh, expires

    def _request(self, method: str, path: str, body=None) -> dict:
        token = self._authenticate()
        status, payload = self._send(method, path, body, token)
        if status == 401:
            token = self._authenticate(failed_token=token)
            status, payload = self._send(method, path, body, token)
        if method == "DELETE" and status in (204, 404):
            return {}
        if status == {"POST": 201, "GET": 200, "PUT": 200}.get(method):
            if self._contains_identity(payload):
                raise CappError("malformed_response")
            return payload
        if status == 401:
            raise CappAuthenticationError(status)
        if status == 409:
            raise CappConflictError()
        code = {403: "authorization", 404: "not_found", 408: "timeout",
                429: "unavailable"}.get(status, "unavailable" if status >= 500 else "invalid_request")
        raise CappError(code, status, status in (408, 429) or status >= 500)

    def _contains_identity(self, value) -> bool:
        """Do not expose identity material echoed in an otherwise valid body."""
        if isinstance(value, str):
            return any(secret and secret in value for secret in (
                self._username, self._password, self._access_token, self._refresh_token,
            ))
        if isinstance(value, dict):
            return any(self._contains_identity(k) or self._contains_identity(v)
                       for k, v in value.items())
        if isinstance(value, list):
            return any(self._contains_identity(item) for item in value)
        return False

    def _capp_body(self, spec: CappRequest) -> dict:
        _segment(spec.name)
        if not isinstance(spec.image, str) or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", spec.image):
            raise CappError("invalid_request")
        if not isinstance(spec.config, dict) or spec.config.keys() - _FIELDS:
            raise CappError("invalid_request")
        if spec.config.get("size") and spec.config.get("customResources"):
            raise CappError("invalid_request")
        if spec.config.get("state", "enabled") != "enabled":
            raise CappError("invalid_request")
        return {**copy.deepcopy(spec.config), "name": spec.name, "image": spec.image}

    def _capp_result(self, raw: dict, target, name: str) -> CappResource:
        result = None
        try:
            if raw["name"] != name or raw.get("namespace", self._target(target).wallet_name) != self._target(target).wallet_name:
                raise ValueError
            uid, image = raw["uid"], raw["image"]
            if not isinstance(uid, str) or not uid or not isinstance(image, str) or not image:
                raise ValueError
            version = raw.get("resourceVersion")
            if version is not None and not isinstance(version, str):
                raise ValueError
            conditions = []
            for item in (raw.get("status") or {}).get("conditions", []):
                if item["status"] not in ("True", "False", "Unknown") or not isinstance(item["type"], str):
                    raise ValueError
                # Freeform reason/message can echo secrets. Deliberately omit them.
                source = item.get("source")
                if source not in (None, "capp", "knative", "logging", "route.domainmapping"):
                    raise ValueError
                conditions.append(CappCondition(item["type"], item["status"], source))
            route = raw.get("routeSpec") or {}
            host = route.get("hostname")
            if host and (not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host)):
                raise ValueError
            url = (("https://" if route.get("tlsEnabled", False) else "http://") + host) if host else None
            result = CappResource(name, uid, url, image, tuple(conditions), version)
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        if result is None:
            raise CappError("malformed_response")
        return result

    def create_capp(self, target: CappWalletTarget | str, spec: CappRequest) -> CappResource:
        path = self._path(target, "capps")
        raw = self._request("POST", path, self._capp_body(spec))
        return self._capp_result(raw, target, spec.name)

    def get_capp(self, target: CappWalletTarget | str, name: str) -> CappResource:
        return self._capp_result(self._request("GET", self._path(target, "capps", name)), target, name)

    def update_capp(self, target: CappWalletTarget | str, name: str, spec: CappRequest) -> CappResource:
        path = self._path(target, "capps", name)
        changes = self._capp_body(spec)
        if spec.name != name:
            raise CappError("invalid_request")
        current = self._request("GET", path)
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
        return self._capp_result(self._request("PUT", path, body), target, name)

    def delete_capp(self, wallet: CappWalletTarget | str, name: str) -> None:
        self._request("DELETE", self._path(wallet, "capps", name))

    def _secret_path(self, target, name: str, collection: bool = False) -> str:
        if not isinstance(name, str) or not _OWNED_SECRET.fullmatch(name):
            raise CappError("invalid_target")
        return self._path(target, "secrets", None if collection else name)

    @staticmethod
    def _secret_data(data: dict) -> dict:
        if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            raise CappError("invalid_request")
        return dict(data)

    def _secret_result(self, raw: dict, target, name: str) -> SecretResource:
        namespace = self._target(target).wallet_name
        if (raw.get("name") != name or raw.get("namespace") != namespace
                or not isinstance(raw.get("uid"), str) or not raw["uid"]
                or (raw.get("resourceVersion") is not None
                    and not isinstance(raw["resourceVersion"], str))):
            raise CappError("malformed_response")
        # Never return Secret data or arbitrary labels/annotations to callers.
        return SecretResource(name, namespace, raw["uid"], raw.get("resourceVersion"))

    def create_secret(self, target: CappWalletTarget | str, spec: SecretRequest) -> SecretResource:
        path = self._secret_path(target, spec.name, collection=True)
        if spec.type != "Opaque":
            raise CappError("invalid_request")
        body = {"name": spec.name, "type": spec.type, "data": self._secret_data(spec.data)}
        return self._secret_result(self._request("POST", path, body), target, spec.name)

    def get_secret(self, target: CappWalletTarget | str, name: str) -> SecretResource:
        return self._secret_result(self._request("GET", self._secret_path(target, name)), target, name)

    def update_secret(self, target: CappWalletTarget | str, name: str, spec: SecretUpdateRequest) -> SecretResource:
        path = self._secret_path(target, name)
        body = {"data": self._secret_data(spec.data)}
        self.get_secret(target, name)
        return self._secret_result(self._request("PUT", path, body), target, name)

    def delete_secret(self, wallet: CappWalletTarget | str, name: str) -> None:
        self._request("DELETE", self._secret_path(wallet, name))
