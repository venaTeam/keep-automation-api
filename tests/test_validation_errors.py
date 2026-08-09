"""Golden-fixture guard on the validation-error contract shape (shared with keep-ui)."""
import json
from pathlib import Path

from src.contracts.validation_errors import (
    ErrorCode,
    FieldError,
    ValidationErrorResponse,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_field_error_matches_golden_fixture():
    golden = json.loads((FIXTURES / "field_error.json").read_text())
    error = FieldError(**golden)
    assert error.dict() == golden


def test_error_response_matches_golden_fixture():
    golden = json.loads((FIXTURES / "error_shape_example.json").read_text())
    response = ValidationErrorResponse(**golden)
    assert response.dict() == golden


def test_all_fixture_codes_are_enum_members():
    codes = {c.value for c in ErrorCode}
    for name in ("field_error.json", "error_shape_example.json"):
        payload = json.loads((FIXTURES / name).read_text())
        errors = payload.get("errors", [payload])
        for error in errors:
            assert error["code"] in codes


def test_namespace_invalid_reserved_for_f25():
    assert ErrorCode.NAMESPACE_INVALID.value == "namespace_invalid"


def test_codes_serialize_as_plain_strings():
    error = FieldError(
        field="timeout_seconds",
        code=ErrorCode.TIMEOUT_OUT_OF_RANGE,
        message="must be at most 900",
    )
    assert error.dict()["code"] == "timeout_out_of_range"
    assert isinstance(error.dict()["code"], str)
