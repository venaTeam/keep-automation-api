"""HTTP round-trip tests for the authoring CRUD routes (D13, spec §8.1)."""
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from src.contracts.field_allowlist import MATCHABLE_OPTIONAL, MATCHABLE_REQUIRED

FIXTURES = Path(__file__).parent / "fixtures"

VALID = {
    "name": "restart-payments",
    "namespace": "payments-wallet",
    "script": "def handle(alert):\n    return {}\n",
    "triggers": [
        {"field": "severity", "value": "critical"},
        {"field": "application", "value": "payments"},
    ],
}


# --- create -------------------------------------------------------------

def test_create_returns_201_with_detail(client):
    resp = client.post("/automations", json=VALID)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "restart-payments"
    assert body["matching_state"] == "inactive"
    assert body["build_state"] == "building"
    assert body["script"] == VALID["script"]
    assert body["timeout_seconds"] == 300
    assert body["grace_seconds"] == 300
    assert body["created_by"] == "noauth@keep"


def test_create_invalid_returns_accumulating_400(client):
    payload = {
        **VALID,
        "triggers": [{"field": "message", "value": "x"}],
        "timeout_seconds": 5000,
    }
    resp = client.post("/automations", json=payload)
    assert resp.status_code == 400
    codes = {e["code"] for e in resp.json()["errors"]}
    assert {"triggers_too_few", "unknown_field", "timeout_out_of_range"} <= codes


def test_create_error_shape_matches_contract_fixture(client):
    resp = client.post("/automations", json={**VALID, "triggers": []})
    assert resp.status_code == 400
    golden = json.loads((FIXTURES / "error_shape_example.json").read_text())
    error_keys = set(golden["errors"][0].keys())
    for error in resp.json()["errors"]:
        assert set(error.keys()) == error_keys  # {field, code, message}


def test_missing_required_body_field_uses_contract_shape(client):
    payload = {k: v for k, v in VALID.items() if k != "script"}
    resp = client.post("/automations", json=payload)
    assert resp.status_code == 400
    errors = resp.json()["errors"]
    assert any(e["field"] == "script" and e["code"] == "field_required" for e in errors)


# --- read ---------------------------------------------------------------

def test_get_detail_returns_script_read_back(client):
    created = client.post("/automations", json=VALID).json()
    resp = client.get(f"/automations/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["script"] == VALID["script"]
    assert resp.json()["build_state"] == "building"


def test_get_missing_returns_404(client):
    assert client.get(f"/automations/{uuid4()}").status_code == 404


def test_list_and_filters(client):
    client.post("/automations", json=VALID)
    client.post(
        "/automations",
        json={**VALID, "name": "other", "namespace": "other-wallet"},
    )
    assert len(client.get("/automations").json()["automations"]) == 2
    filtered = client.get("/automations", params={"namespace": "other-wallet"}).json()
    assert len(filtered["automations"]) == 1
    assert "script" not in filtered["automations"][0]  # list rows carry no script
    by_state = client.get("/automations", params={"build_state": "idle"}).json()
    assert by_state["automations"] == []


# --- update -------------------------------------------------------------

def test_put_while_building_returns_409(client, test_engine):
    created = client.post("/automations", json=VALID).json()
    # Not a tautology: assert the row really is mid-build in the DB first.
    with test_engine.connect() as conn:
        build_state = conn.execute(text("SELECT build_state FROM automations")).scalar()
    assert build_state == "building"

    resp = client.put(f"/automations/{created['id']}", json=VALID)
    assert resp.status_code == 409


def test_put_after_build_idle_succeeds_and_relocks(client, test_engine):
    created = client.post("/automations", json=VALID).json()
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET build_state = 'idle'"))

    resp = client.put(
        f"/automations/{created['id']}",
        json={**VALID, "name": "renamed"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed"
    assert resp.json()["build_state"] == "building"
    assert resp.json()["updated_by"] == "noauth@keep"


def test_put_missing_returns_404(client):
    assert client.put(f"/automations/{uuid4()}", json=VALID).status_code == 404


def test_put_invalid_payload_returns_accumulating_400(client, test_engine):
    created = client.post("/automations", json=VALID).json()
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET build_state = 'idle'"))

    resp = client.put(
        f"/automations/{created['id']}",
        json={**VALID, "triggers": [{"field": "message", "value": "x"}]},
    )
    assert resp.status_code == 400
    codes = {e["code"] for e in resp.json()["errors"]}
    assert {"triggers_too_few", "unknown_field"} <= codes


# --- alert schema -------------------------------------------------------

def test_alert_schema_fields_serves_allowlist(client):
    fields = client.get("/alert-schema/fields").json()["fields"]
    names = [f["name"] for f in fields]
    assert names == list(MATCHABLE_REQUIRED) + list(MATCHABLE_OPTIONAL)
    presence = {f["name"]: f["presence"] for f in fields}
    assert presence["application"] == "required"
    assert presence["node_name"] == "optional"
