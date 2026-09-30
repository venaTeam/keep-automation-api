"""DELETE /automations/{id} + exposure-tier guarantees (D18, spec §8.1)."""
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text

from src.api.deps import get_capp_deletion_client, get_git_client, get_registry_client
from src.bl import cascade
from src.main import get_app
from tests.conftest import OTHER_TENANT, TENANT
from tests.fakes import World, deps_for, seed_external
from tests.helpers import VALID, create_built, revisions, row

WALLET = VALID["namespace"]


@pytest.fixture()
def world():
    return World()


@pytest.fixture()
def app_overrides(app_overrides, world):
    deps = deps_for(world)
    return {
        **app_overrides,
        get_capp_deletion_client: lambda: deps.capp,
        get_registry_client: lambda: deps.registry,
        get_git_client: lambda: deps.git,
    }


def test_delete_returns_202_deleting_then_background_completes(
    client, test_engine, world, reload_publisher
):
    automation_id = create_built(client, test_engine)
    seed_external(world, automation_id, WALLET)

    resp = client.delete(f"/automations/{automation_id}")

    assert resp.status_code == 202
    body = resp.json()
    assert body["id"] == automation_id
    assert body["matching_state"] == "deleting"
    assert body["delete_cascade_step"] == 1
    assert body["index_generation"] == 1
    # Step-1 reload published after commit.
    assert reload_publisher.snapshots[0][automation_id] == ("deleting", 1)
    # TestClient runs the background attempt before returning.
    final = client.get(f"/automations/{automation_id}").json()
    assert final["matching_state"] == "deleted"
    assert final["delete_cascade_step"] == 5
    assert world.capps == set() and world.secrets == set()


def test_delete_already_deleted_returns_200(client, test_engine, world):
    automation_id = create_built(client, test_engine)
    client.delete(f"/automations/{automation_id}")
    calls = len(world.calls)

    resp = client.delete(f"/automations/{automation_id}")

    assert resp.status_code == 200
    assert resp.json()["matching_state"] == "deleted"
    assert len(world.calls) == calls


def test_repeated_delete_reports_progress_and_starts_no_attempt(
    client, client_as, test_engine, world, reload_publisher
):
    """Polling with DELETE must not pile up cascade attempts against CAPP."""
    automation_id = create_built(client, test_engine)
    world.failures["delete_secret"] = 1

    first = client.delete(f"/automations/{automation_id}")
    assert first.status_code == 202
    assert row(test_engine, automation_id).delete_cascade_step == 1
    calls_after_first = list(world.calls)

    for _ in range(3):
        again = client_as(TENANT).delete(f"/automations/{automation_id}")
        assert again.status_code == 202
        assert again.json()["matching_state"] == "deleting"
        assert again.json()["delete_cascade_step"] == 1

    assert world.calls == calls_after_first  # no CAPP / registry / git calls
    assert reload_publisher.count == 1
    assert row(test_engine, automation_id).matching_state == "deleting"
    assert row(test_engine, automation_id).updated_by == "noauth@keep"
    assert [r.action for r in revisions(test_engine, automation_id)].count("delete") == 1


def test_stopped_cascade_is_resumed_by_the_reconciler_path_not_delete(
    client, test_engine, world
):
    automation_id = create_built(client, test_engine)
    world.failures["delete_capp"] = 1
    client.delete(f"/automations/{automation_id}")
    assert row(test_engine, automation_id).matching_state == "deleting"

    result = cascade.resume_delete(UUID(automation_id), deps_for(world))

    assert result.completed
    resp = client.delete(f"/automations/{automation_id}")
    assert resp.status_code == 200
    assert resp.json()["matching_state"] == "deleted"


def test_delete_while_building_is_409_with_no_side_effects(client, test_engine, world, reload_publisher):
    created = client.post("/automations", json=VALID).json()

    resp = client.delete(f"/automations/{created['id']}")

    assert resp.status_code == 409
    assert row(test_engine, created["id"]).matching_state == "inactive"
    assert row(test_engine, created["id"]).delete_cascade_step is None
    assert world.calls == []
    assert reload_publisher.count == 0


def test_delete_unknown_is_404(client):
    assert client.delete(f"/automations/{uuid4()}").status_code == 404


def test_delete_other_tenants_automation_is_404_and_untouched(client_as, test_engine, world):
    automation_id = create_built(client_as(OTHER_TENANT), test_engine)
    resp = client_as(TENANT).delete(f"/automations/{automation_id}")
    assert resp.status_code == 404
    assert row(test_engine, automation_id).matching_state == "inactive"
    assert world.calls == []


def test_delete_active_automation_stops_matching_first(client, test_engine, world, reload_publisher):
    automation_id = create_built(client, test_engine)
    client.post(f"/automations/{automation_id}/enable")
    world.failures["delete_capp"] = -1  # CAPP down: cascade parks at step 1

    resp = client.delete(f"/automations/{automation_id}")

    assert resp.status_code == 202
    current = row(test_engine, automation_id)
    assert (current.matching_state, current.index_generation) == ("deleting", 2)
    assert reload_publisher.snapshots[-1][automation_id] == ("deleting", 2)


def test_delete_response_carries_no_script_or_secrets(client, test_engine):
    automation_id = create_built(client, test_engine)
    body = client.delete(f"/automations/{automation_id}").json()
    assert set(body) == {
        "id",
        "matching_state",
        "build_state",
        "active_digest",
        "delete_cascade_step",
        "index_generation",
        "updated_by",
        "updated_at",
    }


def test_deleted_automation_stays_listed_for_audit(client, test_engine):
    automation_id = create_built(client, test_engine)
    client.delete(f"/automations/{automation_id}")
    listed = client.get("/automations", params={"matching_state": "deleted"}).json()
    assert [a["id"] for a in listed["automations"]] == [automation_id]


def test_only_three_lifecycle_routes_and_no_resume_or_deboard_route():
    paths = {
        (method, route.path)
        for route in get_app().routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert ("POST", "/automations/{automation_id}/enable") in paths
    assert ("POST", "/automations/{automation_id}/disable") in paths
    assert ("DELETE", "/automations/{automation_id}") in paths
    joined = " ".join(path for _, path in paths)
    assert "resume-delete" not in joined
    assert "deboard" not in joined
