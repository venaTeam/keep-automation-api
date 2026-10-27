"""Event-loop and checkpoint guarantees, independent of database provisioning."""

import asyncio
import copy
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.bl import cascade
from src.models.db.automation import MatchingState
from tests.fakes import World, deps_for, seed_external


@pytest.fixture(autouse=True)
def clean_tables():
    yield


@pytest.fixture
def state(monkeypatch):
    loop_thread = threading.get_ident()
    row = SimpleNamespace(
        id=uuid4(),
        namespace="team",
        capp_deployment_id=None,
        matching_state=MatchingState.DELETING,
        delete_cascade_step=1,
        index_generation=1,
        updated_by="operator",
    )
    calls = []

    def snapshot(automation_id):
        assert threading.get_ident() != loop_thread
        calls.append("snapshot")
        return copy.copy(row)

    def checkpoint(automation_id, completed, **values):
        assert threading.get_ident() != loop_thread
        calls.append(f"checkpoint-{completed}")
        if row.delete_cascade_step != completed - 1:
            return False
        row.delete_cascade_step = completed
        for key, value in values.items():
            setattr(row, key, value)
        return True

    monkeypatch.setattr(cascade, "_snapshot", snapshot)
    monkeypatch.setattr(cascade, "_checkpoint", checkpoint)
    return row, calls, loop_thread


async def test_cascade_awaits_capp_and_offloads_sync_steps(state):
    row, calls, loop_thread = state
    world = World()
    seed_external(world, row.id, row.namespace)
    deps = deps_for(world)

    class Capp:
        async def delete_capp(self, wallet, name):
            assert threading.get_ident() == loop_thread
            assert row.delete_cascade_step == 1
            await asyncio.sleep(0)
            await deps.capp.delete_capp(wallet, name)
            calls.append("delete-capp")

        async def delete_secret(self, wallet, name):
            assert threading.get_ident() == loop_thread
            assert row.delete_cascade_step == 1
            await asyncio.sleep(0)
            await deps.capp.delete_secret(wallet, name)
            calls.append("delete-secret")

    class Registry:
        def list_image_entries(self, automation_id):
            assert threading.get_ident() != loop_thread
            return deps.registry.list_image_entries(automation_id)

        def delete_image(self, automation_id, entry):
            assert threading.get_ident() != loop_thread
            deps.registry.delete_image(automation_id, entry)

    result = await cascade.run_cascade(
        row.id, cascade.CascadeDeps(Capp(), Registry(), deps.git, deps.publisher)
    )
    assert result.completed
    assert (
        calls.index("delete-capp")
        < calls.index("delete-secret")
        < calls.index("checkpoint-2")
    )
    assert row.delete_cascade_step == 5
    assert not world.capps and not world.secrets


async def test_cancelled_capp_call_does_not_advance_checkpoint_and_can_resume(state):
    row, calls, _ = state
    world = World()
    seed_external(world, row.id, row.namespace)
    deps = deps_for(world)
    started = asyncio.Event()

    class Capp:
        async def delete_capp(self, wallet, name):
            started.set()
            await asyncio.Event().wait()

        async def delete_secret(self, wallet, name):
            pytest.fail("Secret deletion must not run after cancellation")

    task = asyncio.create_task(
        cascade.run_cascade(
            row.id, cascade.CascadeDeps(Capp(), deps.registry, deps.git, deps.publisher)
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert row.delete_cascade_step == 1
    assert "checkpoint-2" not in calls
    assert (await cascade.resume_delete(row.id, deps)).completed


async def test_async_secret_failure_retains_checkpoint_and_resumes(state):
    row, calls, _ = state
    world = World()
    seed_external(world, row.id, row.namespace)
    world.failures["delete_secret"] = 1
    result = await cascade.run_cascade(row.id, deps_for(world))
    assert result.error == "run_auth_secret_delete_failed"
    assert row.delete_cascade_step == 1
    assert "checkpoint-2" not in calls
    assert (await cascade.resume_delete(row.id, deps_for(world))).completed


async def test_starlette_background_task_awaits_full_cascade(state):
    from starlette.background import BackgroundTask

    row, _, _ = state
    world = World()
    seed_external(world, row.id, row.namespace)
    await BackgroundTask(cascade.run_cascade_in_background, row.id, deps_for(world))()
    assert row.matching_state == MatchingState.DELETED
    assert row.delete_cascade_step == 5


async def test_deboard_offloads_intake_and_awaits_each_cascade(state, monkeypatch):
    row, _, loop_thread = state
    world = World()
    seed_external(world, row.id, row.namespace)

    def wallet_ids(tenant, wallet):
        assert threading.get_ident() != loop_thread
        assert (tenant, wallet) == ("tenant", row.namespace)
        return [row.id]

    def begin_delete(tenant, automation_id, actor, publisher):
        assert threading.get_ident() != loop_thread
        assert automation_id == row.id

    monkeypatch.setattr(cascade, "_wallet_automation_ids", wallet_ids)
    monkeypatch.setattr(cascade, "begin_delete", begin_delete)
    result = await cascade.deboard_wallet(
        "tenant", row.namespace, "operator", deps_for(world)
    )
    assert result.deleted == (row.id,)
