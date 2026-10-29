"""Async CAPP HTTP transport: identity custody, authentication and deadlines."""

import asyncio
import json
import math
from collections.abc import Callable
from datetime import datetime, timezone
from http import HTTPMethod, HTTPStatus
from typing import Any
from urllib.parse import urlsplit

import httpx

from src.bl.capp_errors import CappError, CappErrorCode, error_for_status
from src.bl.capp_responses import required_string
from src.bl.capp_validation import encode_request

JsonObject = dict[str, Any]
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_TOKEN_REFRESH_SKEW_SECONDS = 30
_SUCCESS_STATUS = {
    HTTPMethod.POST: HTTPStatus.CREATED,
    HTTPMethod.GET: HTTPStatus.OK,
    HTTPMethod.PUT: HTTPStatus.OK,
}


class CappTransport:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
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
        self._base_url = base_url.rstrip("/")
        self._username, self._password = username, password
        self._timeout = timeout_seconds
        self._http = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._auth_lock = asyncio.Lock()
        self._access_token = self._refresh_token = ""
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)

    async def aclose(self) -> None:
        """Release the connection pool when its owning application shuts down."""
        await self._http.aclose()

    async def _send(
        self,
        method: HTTPMethod,
        path: str,
        body: JsonObject | None = None,
        token: str = "",
    ) -> tuple[int, JsonObject]:
        content = encode_request(body)
        error = CappError(CappErrorCode.MALFORMED_RESPONSE)
        try:
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            # Bound total elapsed time as well as connect/read/write/pool waits.
            # A slow trickle must not evade the request deadline.
            async with asyncio.timeout(self._timeout):
                async with self._http.stream(
                    method,
                    self._base_url + path,
                    headers=headers,
                    content=content,
                    timeout=self._timeout,
                ) as response:
                    status = response.status_code
                    if (
                        status >= HTTPStatus.MULTIPLE_CHOICES
                        or status == HTTPStatus.NO_CONTENT
                    ):
                        return status, {}
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise CappError(CappErrorCode.MALFORMED_RESPONSE)
                        raw.extend(chunk)
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        return status, payload
        except (TimeoutError, httpx.TimeoutException):
            error = CappError(CappErrorCode.TIMEOUT, retryable=True)
        except (httpx.RequestError, OSError):
            error = CappError(CappErrorCode.UNAVAILABLE, retryable=True)
        except (ValueError, TypeError, UnicodeError):
            error = CappError(CappErrorCode.MALFORMED_RESPONSE)
        # Raise outside handlers so raw transport errors aren't retained as context.
        # Cancellation is deliberately not caught; async callers own task lifetime.
        raise error

    async def _authenticate(self, failed_token: str | None = None) -> str:
        async with self._auth_lock:
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
                    await self._login_request(
                        "refresh", {"refreshToken": self._refresh_token}
                    )
                    return self._access_token
                except CappError:
                    self._refresh_token = ""
            await self._login_request(
                "login", {"username": self._username, "password": self._password}
            )
            return self._access_token

    async def _login_request(self, operation: str, body: JsonObject) -> None:
        status, payload = await self._send(
            HTTPMethod.POST, f"/api/v1/auth/{operation}", body
        )
        if status != HTTPStatus.OK:
            raise error_for_status(status)
        access = required_string(payload, "accessToken")
        refresh = required_string(payload, "refreshToken")
        expires_text = required_string(payload, "expiresAt")
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

    async def request(
        self, method: HTTPMethod, path: str, body: JsonObject | None = None
    ) -> JsonObject:
        encode_request(body)  # Reject invalid input before any authentication I/O.
        token = await self._authenticate()
        status, payload = await self._send(method, path, body, token)
        if status == HTTPStatus.UNAUTHORIZED:
            token = await self._authenticate(failed_token=token)
            status, payload = await self._send(method, path, body, token)
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
