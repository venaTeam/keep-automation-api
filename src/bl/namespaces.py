"""Namespace (wallet_name) validation.

RELAXED until F25 lands: the onboarded-wallet registry doesn't exist yet, so
any non-blank value passes. The `namespace_invalid` error code is already
reserved in the contracts so the UI can bind before F25.

TODO(F25): validate against the onboarded-wallet set (spec §4.1/§5.5) — replace
the blank check with a registry lookup; keep the same error code.
"""
from src.models.api.validation_errors import ErrorCode, FieldError


def validate_namespace(namespace: str) -> FieldError | None:
    if not namespace or not namespace.strip():
        return FieldError(
            field="namespace",
            code=ErrorCode.NAMESPACE_INVALID,
            message="namespace (wallet name) must not be blank.",
        )
    return None
