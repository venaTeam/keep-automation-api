"""API model boundary tests: shape, state exposure, script byte cap."""
import pytest
from pydantic import ValidationError

from src.contracts.limits import SCRIPT_MAX_BYTES
from src.models.api.automation import AutomationIn, AutomationOut

VALID_PAYLOAD = {
    "name": "restart-payments",
    "namespace": "payments-wallet",
    "script": "def handle(alert):\n    return {}\n",
    "triggers": [
        {"field": "severity", "value": "critical"},
        {"field": "application", "value": "payments"},
    ],
}


def test_valid_payload_parses():
    model = AutomationIn(**VALID_PAYLOAD)
    assert model.name == "restart-payments"
    assert model.timeout_seconds is None  # default applied in BL, not here


def test_oversized_script_rejected_at_model_boundary():
    payload = {**VALID_PAYLOAD, "script": "#" * (SCRIPT_MAX_BYTES + 1)}
    with pytest.raises(ValidationError):
        AutomationIn(**payload)


def test_script_cap_counts_bytes_not_chars():
    # Multibyte chars: fewer chars than the cap but more bytes — must reject.
    payload = {**VALID_PAYLOAD, "script": "€" * (SCRIPT_MAX_BYTES // 3 + 1)}
    with pytest.raises(ValidationError):
        AutomationIn(**payload)


def test_automation_out_exposes_both_state_axes():
    fields = AutomationOut.__fields__
    assert "matching_state" in fields
    assert "build_state" in fields
    assert "script" in fields
