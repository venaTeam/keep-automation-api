"""Enable / disable routes (D18, spec §5.3, §8.1)."""
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.conftest import OTHER_TENANT, TENANT
from tests.helpers import DIGEST, VALID, create_built, revisions, row

FIXTURES = Path(__file__).parent / "fixtures"


# --- enable -------------------------------------------------------------


def test_enable_unbuilt_returns_400_active_digest_required(client, test_engine, reload_publisher):
    created = client.post("/automations", json=VALID).json()
    before = row(test_engine, created["id"])

    resp = client.post(f"/automations/{created['id']}/enable")

    assert resp.status_code == 400
    assert resp.json()["errors"] == [
        {
            "field": "active_digest",
            "code": "active_digest_required",
            "message": resp.json()["errors"][0]["message"],
        }
    ]
    after = row(test_engine, created["id"])
    assert after.matching_state == "inactive"
    assert after.index_generation == before.index_generation
    assert [r.action for r in revisions(test_engine, created["id"])] == ["create"]
    assert reload_publisher.count == 0


def test_enable_unbuilt_matches_golden_fixture(client):
    created = client.post("/automations", json=VALID).json()
    resp = client.post(f"/automations/{created['id']}/enable")
    golden = json.loads((FIXTURES / "active_digest_required_error.json").read_text())
    assert resp.json() == golden


def test_enable_blank_digest_is_unbuilt(client, test_engine):
    automation_id = create_built(client, test_engine, digest="   ")
    assert client.post(f"/automations/{automation_id}/enable").status_code == 400


def test_enable_built_activates_bumps_generation_audits_and_publishes(
    client, test_engine, reload_publisher
):
    automation_id = create_built(client, test_engine)

    resp = client.post(f"/automations/{automation_id}/enable")

    assert resp.status_code == 200
    body = resp.json()
    assert body["matching_state"] == "active"
    assert body["index_generation"] == 1
    assert body["active_digest"] == DIGEST
    assert body["updated_by"] == "noauth@keep"
    assert "script" not in body
    enable = revisions(test_engine, automation_id)[-1]
    assert (enable.action, enable.actor, enable.git_sha) == ("enable", "noauth@keep", None)
    assert enable.resulting_digest == DIGEST
    # Published once, and only after commit: the snapshot taken inside the
    # publish already sees the committed state.
    assert reload_publisher.snapshots == [{automation_id: ("active", 1)}]


def test_enable_allowed_while_building_when_digest_present(client, test_engine):
    """An edit's build must not stop the live digest being toggled (§5.3)."""
    automation_id = create_built(client, test_engine)
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET build_state = 'building'"))
    assert client.post(f"/automations/{automation_id}/enable").status_code == 200


# --- disable ------------------------------------------------------------


def test_disable_deactivates_bumps_generation_audits_and_publishes(
    client, test_engine, reload_publisher
):
    automation_id = create_built(client, test_engine)
    client.post(f"/automations/{automation_id}/enable")

    resp = client.post(f"/automations/{automation_id}/disable")

    assert resp.status_code == 200
    assert resp.json()["matching_state"] == "inactive"
    assert resp.json()["index_generation"] == 2
    assert [r.action for r in revisions(test_engine, automation_id)] == [
        "create",
        "enable",
        "disable",
    ]
    assert reload_publisher.snapshots[-1] == {automation_id: ("inactive", 2)}


def test_disable_does_not_require_a_digest(client):
    created = client.post("/automations", json=VALID).json()
    assert client.post(f"/automations/{created['id']}/disable").status_code == 200


def test_repeated_toggle_keeps_state_and_still_bumps_audits_publishes(
    client, test_engine, reload_publisher
):
    automation_id = create_built(client, test_engine)
    client.post(f"/automations/{automation_id}/enable")
    resp = client.post(f"/automations/{automation_id}/enable")

    assert resp.json()["matching_state"] == "active"
    assert resp.json()["index_generation"] == 2
    assert [r.action for r in revisions(test_engine, automation_id)].count("enable") == 2
    assert reload_publisher.count == 2


# --- lifecycle conflicts ------------------------------------------------


@pytest.mark.parametrize("state", ["deleting", "deleted"])
@pytest.mark.parametrize("action", ["enable", "disable"])
def test_toggle_on_deleting_or_deleted_is_409(
    client, test_engine, reload_publisher, state, action
):
    automation_id = create_built(client, test_engine)
    with test_engine.begin() as conn:
        conn.execute(
            text("UPDATE automations SET matching_state = :s"), {"s": state}
        )

    resp = client.post(f"/automations/{automation_id}/{action}")

    assert resp.status_code == 409
    assert resp.json()["matching_state"] == state
    assert row(test_engine, automation_id).matching_state == state
    assert reload_publisher.count == 0


@pytest.mark.parametrize("state", ["deleting", "deleted"])
def test_put_on_deleting_or_deleted_is_409(client, test_engine, state):
    automation_id = create_built(client, test_engine)
    with test_engine.begin() as conn:
        conn.execute(
            text("UPDATE automations SET matching_state = :s"), {"s": state}
        )
    resp = client.put(f"/automations/{automation_id}", json=VALID)
    assert resp.status_code == 409
    assert row(test_engine, automation_id).build_state == "idle"


# --- tenancy ------------------------------------------------------------


@pytest.mark.parametrize("action", ["enable", "disable"])
def test_toggle_unknown_id_is_404(client, action):
    assert client.post(f"/automations/{uuid4()}/{action}").status_code == 404


@pytest.mark.parametrize("action", ["enable", "disable"])
def test_toggle_other_tenants_id_is_404_and_untouched(
    client_as, test_engine, action
):
    owner = client_as(OTHER_TENANT)
    automation_id = create_built(owner, test_engine)

    resp = client_as(TENANT).post(f"/automations/{automation_id}/{action}")

    assert resp.status_code == 404
    assert resp.json() == client_as(TENANT).post(
        f"/automations/{uuid4()}/{action}"
    ).json()
    assert row(test_engine, automation_id).index_generation == 0


def test_cross_tenant_conflict_state_still_404_not_409(client_as, test_engine):
    automation_id = create_built(client_as(OTHER_TENANT), test_engine)
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET matching_state = 'deleted'"))
    resp = client_as(TENANT).post(f"/automations/{automation_id}/enable")
    assert resp.status_code == 404


def test_detail_exposes_lifecycle_progress(client, test_engine):
    automation_id = create_built(client, test_engine)
    client.post(f"/automations/{automation_id}/enable")
    body = client.get(f"/automations/{automation_id}").json()
    assert body["index_generation"] == 1
    assert body["delete_cascade_step"] is None
