"""Table-driven tests for the accumulating authoring validators (spec §4.1–4.2)."""
from src.bl.validation import (
    validate_automation,
    validate_cooldown,
    validate_grace,
    validate_timeout,
    validate_triggers,
)
from src.models.field_allowlist import MATCHABLE_FIELDS
from src.models.validation_limits import (
    COOLDOWN_SECONDS_MAX,
    GRACE_SECONDS_MAX,
    GRACE_SECONDS_MIN,
    TIMEOUT_SECONDS_MAX,
    TRIGGER_VALUE_MAX_BYTES,
    TRIGGERS_MAX,
)
from src.models.api.validation_errors import ErrorCode
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


# --- input bounding (anti-amplification) --------------------------------

def test_one_trigger_per_matchable_field_passes():
    triggers = [{"field": f, "value": "x"} for f in sorted(MATCHABLE_FIELDS)]
    assert validate_triggers(triggers) == []

def test_over_max_triggers_yield_exactly_one_error():
    triggers = [{"field": "severity", "value": "x"}] * (TRIGGERS_MAX + 1)
    errors = validate_triggers(triggers)
    assert codes(errors) == [ErrorCode.TRIGGERS_TOO_MANY.value]

def test_huge_trigger_list_cannot_amplify_the_error_response():
    # Regression: 200k malformed entries used to produce ~200k FieldErrors —
    # a small request amplified into a huge 400 (worker memory/CPU DoS).
    triggers = [{"field": "nope", "value": "x"}] * 200_000
    errors = validate_triggers(triggers)
    assert len(errors) == 1
    assert errors[0].code == ErrorCode.TRIGGERS_TOO_MANY.value

def test_trigger_value_at_byte_cap_ok():
    triggers = [
        {"field": "severity", "value": "a" * TRIGGER_VALUE_MAX_BYTES},
        {"field": "application", "value": "payments"},
    ]
    assert validate_triggers(triggers) == []

def test_trigger_value_over_byte_cap_rejected_with_path():
    triggers = [
        {"field": "severity", "value": "a" * (TRIGGER_VALUE_MAX_BYTES + 1)},
        {"field": "application", "value": "payments"},
    ]
    errors = validate_triggers(triggers)
    assert codes(errors) == [ErrorCode.TRIGGER_VALUE_TOO_LONG.value]
    assert errors[0].field == "triggers[0].value"

def test_trigger_value_cap_counts_bytes_not_characters():
    # "€" is 3 UTF-8 bytes; this stays under the cap in characters but not bytes.
    over_in_bytes = "€" * (TRIGGER_VALUE_MAX_BYTES // 3 + 1)
    triggers = [
        {"field": "severity", "value": over_in_bytes},
        {"field": "application", "value": "payments"},
    ]
    assert ErrorCode.TRIGGER_VALUE_TOO_LONG.value in codes(validate_triggers(triggers))

def test_cooldown_fields_at_max_ok():
    assert validate_cooldown(300, sorted(MATCHABLE_FIELDS)) == []

def test_huge_cooldown_fields_cannot_amplify():
    errors = validate_cooldown(300, ["nope"] * 200_000)
    assert codes(errors) == [ErrorCode.COOLDOWN_FIELDS_TOO_MANY.value]


# --- cooldown -----------------------------------------------------------

def test_cooldown_over_max_rejected():
    errors = validate_cooldown(COOLDOWN_SECONDS_MAX + 1, ["site"])
    assert ErrorCode.COOLDOWN_OUT_OF_RANGE.value in codes(errors)

def test_cooldown_at_max_ok():
    assert validate_cooldown(COOLDOWN_SECONDS_MAX, ["site"]) == []

def test_cooldown_zero_rejected():
    errors = validate_cooldown(0, ["site"])
    assert ErrorCode.COOLDOWN_OUT_OF_RANGE.value in codes(errors)

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
