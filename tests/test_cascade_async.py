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
        if (row.delete_cascade_step or 0) != completed - 1:
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

    class Git:
        def archive(self, automation_id, actor):
            assert threading.get_ident() != loop_thread
            return deps.git.archive(automation_id, actor)

    class Publisher:
        def publish_reload(self):
            assert threading.get_ident() != loop_thread
            deps.publisher.publish_reload()

    row.delete_cascade_step = None
    result = await cascade.run_cascade(
        row.id, cascade.CascadeDeps(Capp(), Registry(), Git(), Publisher())
    )
    assert world.publishes == 1
    assert calls.index("checkpoint-1") < calls.index("delete-capp")
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


@pytest.mark.parametrize(
    "operation,error,checkpoint",
    [
        ("list_images", "registry_list_failed", 2),
        ("delete_image", "registry_delete_failed", 2),
        ("archive", "git_archive_failed", 3),
    ],
)
async def test_blocking_adapter_failure_preserves_checkpoint_and_resumes(
    state, operation, error, checkpoint
):
    row, _, _ = state
    world = World()
    seed_external(world, row.id, row.namespace)
    world.failures[operation] = 1
    result = await cascade.run_cascade(row.id, deps_for(world))
    assert result.error == error
    assert row.delete_cascade_step == checkpoint
    assert row.matching_state == MatchingState.DELETING
    assert (await cascade.resume_delete(row.id, deps_for(world))).completed


@pytest.mark.parametrize(
    "bad_settings",
    [
        {"CAPP_PASSWORD": None},
        {"CAPP_CLUSTER": ""},
        {"CAPP_BASE_URL": "http://capp.example"},
        {"CAPP_REQUEST_TIMEOUT_SECONDS": "not-a-number"},
        {"CAPP_REQUEST_TIMEOUT_SECONDS": "0"},
    ],
)
async def test_delete_admitted_when_capp_configuration_is_invalid(
    state, monkeypatch, bad_settings, caplog
):
    from datetime import datetime, timezone
    import httpx
    from fastapi import FastAPI, Request
    from src.api import deps as api_deps
    from src.api.routes.user.lifecycle_routes import router
    from src.bl.capp.client import CappClient
    from src.models.api.identity import AuthenticatedEntity
    from src.models.db.automation import BuildState
    from src.main import lifespan

    settings = {
        "CAPP_BASE_URL": "https://capp.example",
        "CAPP_CLUSTER": "dev",
        "CAPP_USERNAME": "service-user-sensitive",
        "CAPP_PASSWORD": "password-sensitive",
        "CAPP_REQUEST_TIMEOUT_SECONDS": "7",
    }
    for key, value in {**settings, **bad_settings}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    import runpy
    from src import config

    startup = runpy.run_path(config.__file__)
    for key in settings:
        monkeypatch.setattr(config, key, startup[key])
    row, calls, loop_thread = state
    row.matching_state = MatchingState.ACTIVE
    row.delete_cascade_step = None
    row.build_state = BuildState.IDLE
    row.active_digest = "sha256:" + "a" * 64
    row.updated_at = datetime.now(timezone.utc)
    world = World()
    seed_external(world, row.id, row.namespace)
    deps = deps_for(world)

    def admit(tenant, automation_id, actor, publisher):
        assert threading.get_ident() != loop_thread
        assert automation_id == row.id
        row.matching_state = MatchingState.DELETING
        row.delete_cascade_step = 1
        publisher.publish_reload()
        return cascade.DeleteAdmission(copy.copy(row), started=True)

    monkeypatch.setattr(cascade, "begin_delete", admit)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides.update(
        {
            api_deps.get_authenticated_entity: lambda: AuthenticatedEntity(
                tenant_id="tenant", email="operator"
            ),
            api_deps.require_lifecycle_auth: lambda: None,
            api_deps.get_git_client: lambda: deps.git,
            api_deps.get_registry_client: lambda: deps.registry,
            api_deps.get_reload_publisher: lambda: deps.publisher,
        }
    )
    # Exercise real CAPP dependency resolution, HTTP route and background cascade.
    # Only database admission/checkpoints and unrelated external adapters are faked.
    async with lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.delete(f"/automations/{row.id}")
        assert response.status_code == 202
        assert response.json()["matching_state"] == "deleting"
        assert row.delete_cascade_step == 1
        assert "checkpoint-2" not in calls
        assert world.publishes == 1
        assert world.capps and world.secrets and world.images
        assert isinstance(
            app.state.capp_client, api_deps.UnconfiguredCappDeletionClient
        )
        await api_deps.get_capp_deletion_client(Request({"type": "http", "app": app}))
        warnings = [
            record for record in caplog.records if record.name == api_deps.__name__
        ]
        assert len(warnings) == 1
        code = "invalid_target" if "CAPP_CLUSTER" in bad_settings else "configuration"
        assert warnings[0].getMessage() == f"CAPP client not configured ({code})"
        assert warnings[0].exc_info is None
        assert settings["CAPP_USERNAME"] not in caplog.text
        assert settings["CAPP_PASSWORD"] not in caplog.text
    assert not hasattr(app.state, "capp_client")
    # Simulate corrected startup settings after a restart; fallback owns no pool.
    for key, value in settings.items():
        monkeypatch.setattr(
            config,
            key,
            float(value) if key == "CAPP_REQUEST_TIMEOUT_SECONDS" else value,
        )
    async with lifespan(app):
        actual = await api_deps.get_capp_deletion_client(
            Request({"type": "http", "app": app})
        )
        assert isinstance(actual, CappClient)
        assert actual is app.state.capp_client
    assert not hasattr(app.state, "capp_client")


async def test_created_capp_uid_is_deleted_using_its_deterministic_name(state):
    import httpx
    from src.bl.capp.client import CappClient
    from src.bl.capp.models import CappRequest
    from tests.capp_fakes import NOW, IMAGE, IDENTITY, tokens, capp

    row, _, _ = state
    name = f"automation-{row.id}"
    uid = str(uuid4())
    deletes = []
    world = World()
    seed_external(world, row.id, row.namespace)
    deps = deps_for(world)

    async def handler(request):
        path = request.url.path
        if path.endswith("/login"):
            return httpx.Response(200, json=tokens())
        if request.method == "POST":
            return httpx.Response(
                201, json=capp(name=name, uid=uid, namespace=row.namespace)
            )
        assert request.method == "DELETE"
        deletes.append(path)
        resource_name = path.rsplit("/", 1)[1]
        if "/capps/" in path:
            world.capps.discard((row.namespace, resource_name))
        else:
            world.secrets.discard((row.namespace, resource_name))
        return httpx.Response(204)

    async with CappClient(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        cluster="dev",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as client:
        created = await client.create_capp(row.namespace, CappRequest(name, IMAGE))
        row.capp_deployment_id = created.name
        result = await cascade.run_cascade(
            row.id, cascade.CascadeDeps(client, deps.registry, deps.git, deps.publisher)
        )
    assert row.capp_deployment_id == name
    assert result.completed
    assert deletes == [
        f"/api/v1/clusters/dev/namespaces/{row.namespace}/capps/{name}",
        f"/api/v1/clusters/dev/namespaces/{row.namespace}/secrets/{name}-run-auth",
    ]
    assert not world.capps and not world.secrets
