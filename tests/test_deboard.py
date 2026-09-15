"""deboard_wallet: the cascade fanned out over a tenant's wallet (D18 phase 4)."""
from uuid import UUID

import pytest
from sqlalchemy import text

from src.bl import cascade
from tests.conftest import OTHER_TENANT, TENANT
from tests.fakes import SimulatedFailure, World, deps_for, seed_external
from tests.helpers import VALID, create_built, row

WALLET = "team-wallet"
ACTOR = "ops@keep"


@pytest.fixture()
def world():
    return World()


def _make(client, engine, world, name, wallet=WALLET, **extra) -> UUID:
    automation_id = UUID(
        create_built(client, engine, payload={**VALID, "name": name, "namespace": wallet}, **extra)
    )
    seed_external(world, automation_id, wallet)
    return automation_id


def _set(engine, automation_id, **columns):
    assignments = ", ".join(f"{c} = :{c}" for c in columns)
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE automations SET {assignments} WHERE id = :id"),
            {**columns, "id": str(automation_id)},
        )


def test_mixed_children_are_deleted_skipped_and_reported(client, client_as, test_engine, world):
    active = _make(client, test_engine, world, "active")
    client.post(f"/automations/{active}/enable")
    inactive = _make(client, test_engine, world, "inactive")
    stuck = _make(client, test_engine, world, "stuck-deleting")
    cascade.begin_delete(TENANT, stuck, "original@keep", deps_for(world).publisher)
    already = _make(client, test_engine, world, "already-deleted")
    cascade.begin_delete(TENANT, already, ACTOR, deps_for(world).publisher)
    cascade.run_cascade(already, deps_for(world))
    building = _make(client, test_engine, world, "building", build_state="building")
    world.secrets.add((WALLET, "team-secret"))

    # Same wallet name, other tenant; same tenant, other wallet.
    theirs = _make(client_as(OTHER_TENANT), test_engine, world, "theirs")
    elsewhere = _make(client, test_engine, world, "elsewhere", wallet="other-wallet")

    calls_before = len(world.calls)
    summary = cascade.deboard_wallet(TENANT, WALLET, ACTOR, deps_for(world))

    assert set(summary.deleted) == {active, inactive, stuck}
    assert summary.skipped_building == (building,)
    assert summary.in_progress == () and summary.failed == ()
    assert summary.counts() == {"deleted": 3, "in_progress": 0, "skipped_building": 1, "failed": 0}

    for automation_id in (active, inactive, stuck, already):
        assert row(test_engine, automation_id).matching_state == "deleted"
        assert (WALLET, f"automation-{automation_id}-run-auth") not in world.secrets
    # The already-deleted child was not re-run.
    assert not any(str(already) in str(call) for call in world.calls[calls_before:])
    # Resumed child keeps its original deleter.
    assert row(test_engine, stuck).updated_by == "original@keep"
    # Building child untouched, not cancelled.
    assert row(test_engine, building).matching_state == "inactive"
    assert row(test_engine, building).build_state == "building"
    assert (WALLET, f"automation-{building}") in world.capps
    # Other tenant / other wallet untouched; team Secret survives.
    assert row(test_engine, theirs).matching_state == "inactive"
    assert (WALLET, f"automation-{theirs}-run-auth") in world.secrets
    assert row(test_engine, elsewhere).matching_state == "inactive"
    assert (WALLET, "team-secret") in world.secrets


def test_building_child_deleted_on_a_later_call(client, test_engine, world):
    building = _make(client, test_engine, world, "building", build_state="building")
    assert cascade.deboard_wallet(TENANT, WALLET, ACTOR, deps_for(world)).skipped_building == (building,)

    _set(test_engine, building, build_state="idle")
    summary = cascade.deboard_wallet(TENANT, WALLET, ACTOR, deps_for(world))

    assert summary.deleted == (building,)


def test_one_child_failure_does_not_stop_the_rest_and_resumes(client, test_engine, world):
    bad = _make(client, test_engine, world, "bad")
    good = _make(client, test_engine, world, "good")
    deps = deps_for(world)

    class FailsForBad:
        def delete_capp(self, wallet, name):
            if name == f"automation-{bad}":
                raise SimulatedFailure("403")
            deps.capp.delete_capp(wallet, name)

        def delete_secret(self, wallet, name):
            deps.capp.delete_secret(wallet, name)

    flaky = cascade.CascadeDeps(
        capp=FailsForBad(), registry=deps.registry, git=deps.git, publisher=deps.publisher
    )
    first = cascade.deboard_wallet(TENANT, WALLET, ACTOR, flaky)

    assert first.deleted == (good,)
    assert first.in_progress == (bad,)
    assert row(test_engine, bad).matching_state == "deleting"
    assert row(test_engine, bad).delete_cascade_step == 1

    second = cascade.deboard_wallet(TENANT, WALLET, ACTOR, deps_for(world))
    assert second.deleted == (bad,)
    assert row(test_engine, bad).matching_state == "deleted"


def test_admission_error_is_isolated_as_failed(monkeypatch, client, test_engine, world):
    first = _make(client, test_engine, world, "first")
    second = _make(client, test_engine, world, "second")
    real = cascade.begin_delete

    def flaky_begin(tenant_id, automation_id, actor, publisher):
        if automation_id == first:
            raise RuntimeError("db blip")
        return real(tenant_id, automation_id, actor, publisher)

    monkeypatch.setattr(cascade, "begin_delete", flaky_begin)
    summary = cascade.deboard_wallet(TENANT, WALLET, ACTOR, deps_for(world))

    assert summary.failed == (first,)
    assert summary.deleted == (second,)
    assert row(test_engine, first).matching_state == "inactive"


def test_empty_wallet_succeeds(test_engine, world):
    summary = cascade.deboard_wallet(TENANT, "no-such-wallet", ACTOR, deps_for(world))
    assert summary.counts() == {"deleted": 0, "in_progress": 0, "skipped_building": 0, "failed": 0}
    assert world.calls == []


def test_repeat_after_completion_is_a_no_op(client, test_engine, world):
    _make(client, test_engine, world, "one")
    cascade.deboard_wallet(TENANT, WALLET, ACTOR, deps_for(world))
    calls = len(world.calls)
    summary = cascade.deboard_wallet(TENANT, WALLET, ACTOR, deps_for(world))
    assert summary.counts()["deleted"] == 0
    assert len(world.calls) == calls


def test_deboard_only_calls_per_automation_capp_operations(client, test_engine, world):
    """No namespace / quota / permission mutation exists in the adapter surface."""
    _make(client, test_engine, world, "one")
    cascade.deboard_wallet(TENANT, WALLET, ACTOR, deps_for(world))
    assert set(world.operations()) <= {
        "publish_reload",
        "delete_capp",
        "delete_secret",
        "list_images",
        "delete_image",
        "archive",
    }
