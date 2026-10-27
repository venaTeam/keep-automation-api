"""Public CAPP failures contain only stable codes and HTTP metadata."""

from enum import StrEnum
from http import HTTPStatus


class CappErrorCode(StrEnum):
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"
    INVALID_TARGET = "invalid_target"
    INVALID_REQUEST = "invalid_request"
    CONFIGURATION = "configuration"


class CappError(RuntimeError):
    def __init__(
        self,
        code: CappErrorCode,
        http_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        super().__init__(
            f"CAPP {code}" + (f" (HTTP {http_status})" if http_status else "")
        )


class CappAuthenticationError(CappError):
    def __init__(self, http_status: int | None = None) -> None:
        super().__init__(CappErrorCode.AUTHENTICATION, http_status)


class CappConflictError(CappError):
    def __init__(self) -> None:
        super().__init__(CappErrorCode.CONFLICT, HTTPStatus.CONFLICT)


def error_for_status(status: int) -> CappError:
    """Classify upstream status without retaining its body or reason phrase.

    Keep unknown status integers valid: proxies may return nonstandard codes.
    """
    if status == HTTPStatus.UNAUTHORIZED:
        return CappAuthenticationError(status)
    if status == HTTPStatus.CONFLICT:
        return CappConflictError()
    codes = {
        HTTPStatus.FORBIDDEN: CappErrorCode.AUTHORIZATION,
        HTTPStatus.NOT_FOUND: CappErrorCode.NOT_FOUND,
        HTTPStatus.REQUEST_TIMEOUT: CappErrorCode.TIMEOUT,
        HTTPStatus.TOO_MANY_REQUESTS: CappErrorCode.UNAVAILABLE,
    }
    server_error = HTTPStatus.INTERNAL_SERVER_ERROR <= status < 600
    default = (
        CappErrorCode.UNAVAILABLE if server_error else CappErrorCode.INVALID_REQUEST
    )
    retryable = server_error or status in (
        HTTPStatus.REQUEST_TIMEOUT,
        HTTPStatus.TOO_MANY_REQUESTS,
    )
    return CappError(codes.get(status, default), status, retryable)
