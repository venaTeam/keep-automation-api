"""Async CAPP HTTP transport: identity custody, authentication and deadlines."""

import asyncio
import json
import math
import time
from collections.abc import Callable
from datetime import datetime, timezone
from http import HTTPMethod, HTTPStatus
from urllib.parse import urlsplit

import httpx

from src.bl.capp.errors import CappError, CappErrorCode, error_for_status
from src.bl.capp.responses import required_string
from src.bl.capp.models import JsonObject

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_TOKEN_REFRESH_SKEW_SECONDS = 30
_AUTH_FAILURE_COOLDOWN_SECONDS = 5
_SUCCESS_STATUS = {
    HTTPMethod.POST: HTTPStatus.CREATED,
    HTTPMethod.GET: HTTPStatus.OK,
    HTTPMethod.PUT: HTTPStatus.OK,
}


def encode_request(body: JsonObject | None) -> bytes | None:
    """Never retain serialization errors, which can contain request secrets."""
    if body is None:
        return None
    try:
        return json.dumps(body, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        pass
    raise CappError(CappErrorCode.INVALID_REQUEST)


def _validate_base_url(base_url: str) -> str:
    """Normalize HTTPS endpoints and reject credentials or malformed URL parts."""
    valid = False
    try:
        url = urlsplit(base_url)
        _ = url.port  # Access validates nonnumeric and out-of-range ports.
        valid = (
            url.scheme == "https"
            and bool(url.hostname)
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
            and not any(c.isspace() or ord(c) < 32 for c in base_url)
        )
    except (ValueError, TypeError):
        pass
    if not valid:
        raise CappError(CappErrorCode.CONFIGURATION)
    return base_url.rstrip("/")


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
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        try:
            valid = math.isfinite(timeout_seconds) and timeout_seconds > 0
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
        self._username, self._password = username, password
        self._timeout = timeout_seconds
        self._http = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic_clock
        # Store only sanitized metadata, never exceptions or their tracebacks.
        self._auth_failure: tuple[CappErrorCode, int | None, bool] | None = None
        self._auth_retry_at = 0.0
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
        content: bytes | None = None,
        token: str = "",
    ) -> tuple[int, JsonObject]:
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
        except (ValueError, TypeError, UnicodeError, RecursionError):
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
            if (
                self._auth_failure is not None
                and self._monotonic() < self._auth_retry_at
            ):
                code, status, retryable = self._auth_failure
                error = (
                    error_for_status(status)
                    if status is not None
                    else CappError(code, retryable=retryable)
                )
                raise error
            self._access_token = ""
            if self._refresh_token:
                try:
                    await self._login_request(
                        "refresh", {"refreshToken": self._refresh_token}
                    )
                    return self._access_token
                except CappError:
                    self._refresh_token = ""
            failure = None
            try:
                await self._login_request(
                    "login", {"username": self._username, "password": self._password}
                )
            except CappError as exc:
                self._auth_failure = (exc.code, exc.http_status, exc.retryable)
                self._auth_retry_at = self._monotonic() + _AUTH_FAILURE_COOLDOWN_SECONDS
                failure = exc
            if failure is not None:
                raise failure
            return self._access_token

    async def _login_request(self, operation: str, body: JsonObject) -> None:
        status, payload = await self._send(
            HTTPMethod.POST, f"/api/v1/auth/{operation}", encode_request(body)
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
        self._auth_failure = None
        self._auth_retry_at = 0.0

    async def request(
        self, method: HTTPMethod, path: str, body: JsonObject | None = None
    ) -> JsonObject:
        # Encode once before authentication; reuse the snapshot on a 401 retry.
        content = encode_request(body)
        token = await self._authenticate()
        # Snapshot before yielding to HTTP: another request may rotate both tokens.
        request_tokens = (token, self._refresh_token)
        status, payload = await self._send(method, path, content, token)
        if status == HTTPStatus.UNAUTHORIZED:
            token = await self._authenticate(failed_token=token)
            request_tokens += (token, self._refresh_token)
            status, payload = await self._send(method, path, content, token)
        if method == HTTPMethod.DELETE and status in (
            HTTPStatus.NO_CONTENT,
            HTTPStatus.NOT_FOUND,
        ):
            return {}
        if status == _SUCCESS_STATUS.get(method):
            if self._contains_identity(payload, request_tokens):
                raise CappError(CappErrorCode.MALFORMED_RESPONSE)
            return payload
        raise error_for_status(status)

    def _contains_identity(
        self, value: object, request_tokens: tuple[str, ...]
    ) -> bool:
        """Reject echoed passwords/tokens, including those used before refresh.

        A username is an identifier that may legitimately occur in image paths,
        namespaces or platform annotations; it is not a secret substring marker.
        """
        secrets = (
            self._password,
            self._access_token,
            self._refresh_token,
            *request_tokens,
        )
        # JSON can parse successfully at depths that exhaust a recursive scanner.
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                if any(secret and secret in item for secret in secrets):
                    return True
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        return False
