"""Machine-readable field-level 400 shape (automation-contracts.md §Validation errors).

Consumed by keep-ui G28/G29 — the UI keys off `code` (stable tokens), never
`message`. Golden fixtures under tests/fixtures/ are the shared UI artifact.
"""
from enum import Enum

from pydantic import BaseModel


class ErrorCode(str, Enum):
    FIELD_REQUIRED = "field_required"
    INVALID_VALUE = "invalid_value"
    TRIGGERS_TOO_FEW = "triggers_too_few"
    TRIGGERS_DUPLICATE_FIELD = "triggers_duplicate_field"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_TRIGGER_SHAPE = "invalid_trigger_shape"
    COOLDOWN_OUT_OF_RANGE = "cooldown_out_of_range"
    COOLDOWN_FIELDS_REQUIRED = "cooldown_fields_required"
    COOLDOWN_FIELDS_WITHOUT_SECONDS = "cooldown_fields_without_seconds"
    TIMEOUT_OUT_OF_RANGE = "timeout_out_of_range"
    GRACE_OUT_OF_RANGE = "grace_out_of_range"
    LOGSTASH_SCHEME_NOT_HTTPS = "logstash_scheme_not_https"
    LOGSTASH_PRIVATE_ADDRESS = "logstash_private_address"
    LOGSTASH_UNRESOLVABLE = "logstash_unresolvable"
    SCRIPT_TOO_LARGE = "script_too_large"
    SCRIPT_SYNTAX_ERROR = "script_syntax_error"
    HANDLE_MISSING = "handle_missing"
    HANDLE_BAD_ARITY = "handle_bad_arity"
    # Reserved for F25 (onboarded-wallet check); until then fires only on blank.
    NAMESPACE_INVALID = "namespace_invalid"


class FieldError(BaseModel):
    # Dotted/indexed path into the submitted body, e.g. "triggers[1].field".
    field: str
    code: ErrorCode
    message: str

    class Config:
        use_enum_values = True


class ValidationErrorResponse(BaseModel):
    errors: list[FieldError]
