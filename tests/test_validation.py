"""Table-driven tests for the accumulating authoring validators (spec §4.1–4.2)."""
from src.bl.validation import (
    validate_automation,
    validate_cooldown,
    validate_grace,
    validate_timeout,
    validate_triggers,
)
from src.contracts.limits import (
    COOLDOWN_SECONDS_MAX,
    GRACE_SECONDS_MAX,
    GRACE_SECONDS_MIN,
    TIMEOUT_SECONDS_MAX,
)
from src.contracts.validation_errors import ErrorCode
from src.models.api.automation import AutomationIn

TWO_TRIGGERS = [
    {"field": "severity", "value": "critical"},
    {"field": "application", "value": "payments"},
]


def codes(errors):
    return [e.code for e in errors]


# --- triggers -----------------------------------------------------------

def test_two_valid_triggers_pass():
    assert validate_triggers(TWO_TRIGGERS) == []

def test_single_trigger_too_few():
    assert ErrorCode.TRIGGERS_TOO_FEW.value in codes(validate_triggers(TWO_TRIGGERS[:1]))

def test_duplicate_field_rejected():
    triggers = [
        {"field": "severity", "value": "critical"},
        {"field": "severity", "value": "warning"},
    ]
    assert ErrorCode.TRIGGERS_DUPLICATE_FIELD.value in codes(validate_triggers(triggers))

def test_unknown_field_rejected_with_indexed_path():
    triggers = TWO_TRIGGERS + [{"field": "message", "value": "boom"}]
    errors = validate_triggers(triggers)
    assert ErrorCode.UNKNOWN_FIELD.value in codes(errors)
    assert errors[0].field == "triggers[2].field"

def test_operator_shape_rejected():
    triggers = TWO_TRIGGERS + [{"field": "site", "value": "eu", "op": "regex"}]
    assert ErrorCode.INVALID_TRIGGER_SHAPE.value in codes(validate_triggers(triggers))

def test_non_string_value_rejected():
    triggers = TWO_TRIGGERS + [{"field": "site", "value": 42}]
    assert ErrorCode.INVALID_TRIGGER_SHAPE.value in codes(validate_triggers(triggers))


# --- cooldown -----------------------------------------------------------

def test_cooldown_over_max_rejected():
    errors = validate_cooldown(COOLDOWN_SECONDS_MAX + 1, ["site"])
    assert ErrorCode.COOLDOWN_OUT_OF_RANGE.value in codes(errors)

def test_cooldown_at_max_ok():
    assert validate_cooldown(COOLDOWN_SECONDS_MAX, ["site"]) == []

def test_cooldown_seconds_without_fields_rejected():
    errors = validate_cooldown(300, None)
    assert ErrorCode.COOLDOWN_FIELDS_REQUIRED.value in codes(errors)

def test_empty_cooldown_fields_is_whole_automation_and_valid():
    assert validate_cooldown(300, []) == []

def test_cooldown_fields_without_seconds_rejected():
    errors = validate_cooldown(None, ["site"])
    assert ErrorCode.COOLDOWN_FIELDS_WITHOUT_SECONDS.value in codes(errors)

def test_cooldown_field_outside_allowlist_rejected():
    errors = validate_cooldown(300, ["fingerprint"])
    assert ErrorCode.UNKNOWN_FIELD.value in codes(errors)
    assert errors[0].field == "cooldown_fields[0]"


# --- timeout / grace ----------------------------------------------------

def test_timeout_bounds():
    assert validate_timeout(None) == []
    assert validate_timeout(TIMEOUT_SECONDS_MAX) == []
    assert ErrorCode.TIMEOUT_OUT_OF_RANGE.value in codes(
        validate_timeout(TIMEOUT_SECONDS_MAX + 1)
    )
    assert ErrorCode.TIMEOUT_OUT_OF_RANGE.value in codes(validate_timeout(0))

def test_grace_bounds():
    assert validate_grace(None) == []
    assert validate_grace(GRACE_SECONDS_MIN) == []
    assert validate_grace(GRACE_SECONDS_MAX) == []
    assert ErrorCode.GRACE_OUT_OF_RANGE.value in codes(
        validate_grace(GRACE_SECONDS_MIN - 1)
    )
    assert ErrorCode.GRACE_OUT_OF_RANGE.value in codes(
        validate_grace(GRACE_SECONDS_MAX + 1)
    )


# --- accumulation -------------------------------------------------------

def test_multiple_failures_accumulate():
    data = AutomationIn(
        name="broken",
        namespace="   ",
        script="def handle(alert): pass",
        triggers=[{"field": "message", "value": "x"}],
        cooldown_seconds=COOLDOWN_SECONDS_MAX + 1,
        timeout_seconds=TIMEOUT_SECONDS_MAX + 1,
        grace_seconds=GRACE_SECONDS_MAX + 1,
    )
    error_codes = set(codes(validate_automation(data)))
    assert {
        ErrorCode.NAMESPACE_INVALID.value,
        ErrorCode.TRIGGERS_TOO_FEW.value,
        ErrorCode.UNKNOWN_FIELD.value,
        ErrorCode.COOLDOWN_OUT_OF_RANGE.value,
        ErrorCode.COOLDOWN_FIELDS_REQUIRED.value,
        ErrorCode.TIMEOUT_OUT_OF_RANGE.value,
        ErrorCode.GRACE_OUT_OF_RANGE.value,
    } <= error_codes
