"""HTTP round-trip tests for the authoring CRUD routes (D13, spec §8.1)."""
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from src.contracts.field_allowlist import MATCHABLE_OPTIONAL, MATCHABLE_REQUIRED
from tests.conftest import OTHER_TENANT, TENANT

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


# --- tenant isolation ---------------------------------------------------


def test_create_stamps_the_session_tenant_not_the_body(client_as, test_engine):
    """A `tenant_id` in the body is ignored — the session's tenant is stamped."""
    resp = client_as(TENANT).post(
        "/automations", json={**VALID, "tenant_id": OTHER_TENANT}
    )
    assert resp.status_code == 201
    with test_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT tenant_id FROM automations WHERE id = :id"),
            {"id": resp.json()["id"]},
        ).scalar()
    assert stored == TENANT


def test_list_does_not_leak_across_tenants(client_as):
    mine = client_as(TENANT)
    theirs = client_as(OTHER_TENANT)
    mine.post("/automations", json=VALID)
    theirs.post("/automations", json={**VALID, "name": "theirs"})

    assert [a["name"] for a in mine.get("/automations").json()["automations"]] == [
        VALID["name"]
    ]
    assert [a["name"] for a in theirs.get("/automations").json()["automations"]] == [
        "theirs"
    ]


def test_get_of_another_tenants_id_is_404_not_403(client_as):
    theirs = client_as(OTHER_TENANT).post("/automations", json=VALID).json()
    resp = client_as(TENANT).get(f"/automations/{theirs['id']}")
    assert resp.status_code == 404
    # Indistinguishable from a never-existing id: no existence leak (§8.1).
    unknown = client_as(TENANT).get(f"/automations/{uuid4()}")
    assert resp.json() == unknown.json()


def test_put_of_another_tenants_id_is_404_not_409(client_as, test_engine):
    """Even mid-build — a 409 would confirm the row exists."""
    theirs = client_as(OTHER_TENANT).post("/automations", json=VALID).json()
    assert theirs["build_state"] == "building"
    assert (
        client_as(TENANT).put(f"/automations/{theirs['id']}", json=VALID).status_code
        == 404
    )

    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET build_state = 'idle'"))
    assert (
        client_as(TENANT).put(f"/automations/{theirs['id']}", json=VALID).status_code
        == 404
    )
    # The owner can still edit it — the 404s above were the tenant predicate.
    assert (
        client_as(OTHER_TENANT)
        .put(f"/automations/{theirs['id']}", json={**VALID, "name": "renamed"})
        .status_code
        == 200
    )


# --- alert schema -------------------------------------------------------

def test_alert_schema_fields_serves_allowlist(client):
    fields = client.get("/alert-schema/fields").json()["fields"]
    names = [f["name"] for f in fields]
    assert names == list(MATCHABLE_REQUIRED) + list(MATCHABLE_OPTIONAL)
    presence = {f["name"]: f["presence"] for f in fields}
    assert presence["application"] == "required"
    assert presence["node_name"] == "optional"
