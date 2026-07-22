"""Accumulating authoring validators (spec §4.1–4.2).

Every rule appends a FieldError and keeps going — the caller returns ALL
failures in one machine-readable 400 (contracts §Validation errors), so an
author fixes the form in one round trip. Field paths use the dotted/indexed
convention (`triggers[1].field`).

Logstash SSRF and the AST contract check live in their own modules
(`src/bl/ssrf.py`, `src/bl/ast_check.py`) and are stitched in by
`validate_automation`.
"""
from src.bl import ast_check, ssrf
from src.bl.namespaces import validate_namespace
from src.contracts.field_allowlist import MATCHABLE_FIELDS
from src.contracts.limits import (
    COOLDOWN_SECONDS_MAX,
    GRACE_SECONDS_MAX,
    GRACE_SECONDS_MIN,
    TIMEOUT_SECONDS_MAX,
    TRIGGERS_MIN,
)
from src.contracts.validation_errors import ErrorCode, FieldError
from src.models.api.automation import AutomationIn


def validate_triggers(triggers: list) -> list[FieldError]:
    errors: list[FieldError] = []
    if len(triggers) < TRIGGERS_MIN:
        errors.append(
            FieldError(
                field="triggers",
                code=ErrorCode.TRIGGERS_TOO_FEW,
                message=f"At least {TRIGGERS_MIN} conditions are required.",
            )
        )

    seen_fields: dict[str, int] = {}
    for i, condition in enumerate(triggers):
        path = f"triggers[{i}]"
        # Exact-equality shape only: {"field": str, "value": str} — nothing else.
        if (
            not isinstance(condition, dict)
            or set(condition.keys()) != {"field", "value"}
            or not isinstance(condition.get("field"), str)
            or not isinstance(condition.get("value"), str)
        ):
            errors.append(
                FieldError(
                    field=path,
                    code=ErrorCode.INVALID_TRIGGER_SHAPE,
                    message="Each condition must be exactly {field: string, value: string} "
                    "(exact equality — no operators, OR, or nesting).",
                )
            )
            continue

        field_name = condition["field"]
        if field_name not in MATCHABLE_FIELDS:
            errors.append(
                FieldError(
                    field=f"{path}.field",
                    code=ErrorCode.UNKNOWN_FIELD,
                    message=f"'{field_name}' is not a matchable alert field.",
                )
            )
        elif field_name in seen_fields:
            errors.append(
                FieldError(
                    field=f"{path}.field",
                    code=ErrorCode.TRIGGERS_DUPLICATE_FIELD,
                    message=f"Duplicate condition on '{field_name}' — AND of two values "
                    "on one field can never match.",
                )
            )
        else:
            seen_fields[field_name] = i
    return errors


def validate_cooldown(
    cooldown_seconds: int | None, cooldown_fields: list | None
) -> list[FieldError]:
    errors: list[FieldError] = []
    if cooldown_seconds is not None:
        if cooldown_seconds <= 0 or cooldown_seconds > COOLDOWN_SECONDS_MAX:
            errors.append(
                FieldError(
                    field="cooldown_seconds",
                    code=ErrorCode.COOLDOWN_OUT_OF_RANGE,
                    message=f"cooldown_seconds must be in 1..{COOLDOWN_SECONDS_MAX}.",
                )
            )
        # Required-iff-set: absent is an error; an explicit empty list is the
        # documented whole-automation cooldown.
        if cooldown_fields is None:
            errors.append(
                FieldError(
                    field="cooldown_fields",
                    code=ErrorCode.COOLDOWN_FIELDS_REQUIRED,
                    message="cooldown_fields is required when cooldown_seconds is set "
                    "(empty list = whole-automation cooldown).",
                )
            )
    elif cooldown_fields is not None:
        errors.append(
            FieldError(
                field="cooldown_fields",
                code=ErrorCode.COOLDOWN_FIELDS_WITHOUT_SECONDS,
                message="cooldown_fields requires cooldown_seconds.",
            )
        )

    for i, field_name in enumerate(cooldown_fields or []):
        if not isinstance(field_name, str) or field_name not in MATCHABLE_FIELDS:
            errors.append(
                FieldError(
                    field=f"cooldown_fields[{i}]",
                    code=ErrorCode.UNKNOWN_FIELD,
                    message=f"'{field_name}' is not a matchable alert field.",
                )
            )
    return errors


def validate_timeout(timeout_seconds: int | None) -> list[FieldError]:
    if timeout_seconds is None:
        return []
    if timeout_seconds <= 0 or timeout_seconds > TIMEOUT_SECONDS_MAX:
        return [
            FieldError(
                field="timeout_seconds",
                code=ErrorCode.TIMEOUT_OUT_OF_RANGE,
                message=f"timeout_seconds must be in 1..{TIMEOUT_SECONDS_MAX}.",
            )
        ]
    return []


def validate_grace(grace_seconds: int | None) -> list[FieldError]:
    if grace_seconds is None:
        return []
    if grace_seconds < GRACE_SECONDS_MIN or grace_seconds > GRACE_SECONDS_MAX:
        return [
            FieldError(
                field="grace_seconds",
                code=ErrorCode.GRACE_OUT_OF_RANGE,
                message=f"grace_seconds must be in {GRACE_SECONDS_MIN}..{GRACE_SECONDS_MAX}.",
            )
        ]
    return []


def validate_automation(data: AutomationIn) -> list[FieldError]:
    """Run every authoring rule; return ALL failures (possibly empty)."""
    errors: list[FieldError] = []
    namespace_error = validate_namespace(data.namespace)
    if namespace_error:
        errors.append(namespace_error)
    errors.extend(validate_triggers(data.triggers))
    errors.extend(validate_cooldown(data.cooldown_seconds, data.cooldown_fields))
    errors.extend(validate_timeout(data.timeout_seconds))
    errors.extend(validate_grace(data.grace_seconds))
    errors.extend(ssrf.validate_logstash_url(data.logstash_url))
    errors.extend(ast_check.validate_script(data.script))
    return errors
