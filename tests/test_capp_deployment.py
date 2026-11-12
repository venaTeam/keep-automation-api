"""Deployment policy over the real resource client and mocked CAPP HTTP."""

import asyncio
import traceback
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest

from src.bl.capp.client import CappClient
from src.bl.capp.deployment import CappDeploymentCoordinator
from src.bl.capp.errors import CappError
from src.exceptions import CappUnavailableError
from tests.capp_fakes import NOW, NAME, IMAGE, NEXT_IMAGE, IDENTITY, Wire, capp, tokens

AUTOMATION_ID = UUID(NAME.removeprefix("automation-"))
UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture(autouse=True)
def clean_tables():
    yield


def resource(**changes):
    return capp(uid=UID, **changes)


@asynccontextmanager
async def coordinator(replies, **settings):
    wire = Wire(replies)
    async with CappClient(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        cluster="dev",
        timeout_seconds=7,
        clock=lambda: NOW,
        transport=httpx.MockTransport(wire),
    ) as client:
        yield CappDeploymentCoordinator(
            client, poll_interval_seconds=0.001, **settings
        ), wire


def create_args():
    return dict(
        automation_id=AUTOMATION_ID,
        wallet="team",
        image=IMAGE,
        secret_refs=["team-secret"],
    )


def rollout_args(deployment_id=UID):
    return dict(**create_args(), deployment_id=deployment_id)


def managed_env():
    return [
        {"name": "AUTOMATION_ID", "value": str(AUTOMATION_ID)},
        {"name": "AUTOMATION_SECRETS_PATH", "value": "/etc/automation/secrets"},
    ]


def volume(secret="team-secret"):
    return {
        "name": "automation-secrets",
        "secretName": secret,
        "mountPath": "/etc/automation/secrets",
    }


async def test_create_waits_for_matching_image_serving_readiness_and_url():
    ready_conditions = {
        "conditions": [{"type": "Ready", "status": "True", "source": "knative"}]
    }
    replies = [
        (200, tokens()),
        (201, resource()),
        (200, resource(image=NEXT_IMAGE)),
        (
            200,
            resource(
                status={
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True",
                            "source": "route.domainmapping",
                        }
                    ]
                }
            ),
        ),
        (
            200,
            resource(
                status={
                    "conditions": [
                        {"type": "Ready", "status": "False", "source": "knative"}
                    ]
                }
            ),
        ),
        (200, resource(routeSpec={})),
        (200, resource(status=ready_conditions)),
    ]
    async with coordinator(replies) as (client, wire):
        result = await client.create_deployment(**create_args())
    assert result.deployment_id == NAME
    assert result.deployment_url == "https://hello.example"
    assert len(wire.calls) == 7
    body = wire.calls[1][2]
    assert body["env"] == managed_env()
    assert body["secretVolumes"] == [volume()]
    assert all("/secrets" not in call[1] for call in wire.calls)


async def test_create_conflict_reconciles_existing_resource_without_second_create():
    current = resource(image=NEXT_IMAGE)
    async with coordinator(
        [
            (200, tokens()),
            (409, {}),
            (200, current),
            (200, current),
            (200, resource()),
            (200, resource()),
        ]
    ) as (client, wire):
        result = await client.create_deployment(**create_args())
    assert result.deployment_id == NAME
    assert [call[0] for call in wire.calls] == [
        "POST",
        "POST",
        "GET",
        "GET",
        "PUT",
        "GET",
    ]
    assert wire.calls[4][2]["image"] == IMAGE


async def test_replay_of_accepted_create_does_not_restart_revision():
    current = resource(env=managed_env(), secretVolumes=[volume()])
    async with coordinator(
        [(200, tokens()), (409, {}), (200, current), (200, current)]
    ) as (client, wire):
        await client.create_deployment(**create_args())
    assert not any(call[0] == "PUT" for call in wire.calls)


@pytest.mark.parametrize("stored_id", [UID, NAME])
@pytest.mark.parametrize("secret_refs", [["replacement"], []])
async def test_rollout_preserves_system_config_and_replaces_or_removes_team_secret(
    stored_id, secret_refs
):
    system_env = {"name": "AUTOMATION_RUN_API_KEY", "value": "preserved-key"}
    system_volume = {
        "name": "run-auth",
        "secretName": NAME + "-run-auth",
        "mountPath": "/run/auth",
    }
    current = resource(
        image=NEXT_IMAGE,
        env=[system_env, *managed_env()],
        secretVolumes=[system_volume, volume("old-secret")],
        scaleSpec={"minReplicas": 2},
    )
    async with coordinator(
        [
            (200, tokens()),
            (200, current),
            (200, current),
            (200, resource()),
            (200, resource()),
        ]
    ) as (client, wire):
        args = rollout_args(stored_id)
        args["secret_refs"] = secret_refs
        await client.rollout(**args)
    updates = [call for call in wire.calls if call[0] == "PUT"]
    assert len(updates) == 1
    assert updates[0][1].endswith("/capps/" + NAME)
    body = updates[0][2]
    assert body["env"] == [system_env, *managed_env()]
    assert body["secretVolumes"] == [system_volume] + (
        [volume("replacement")] if secret_refs else []
    )
    assert body["scaleSpec"] == {"minReplicas": 2}


async def test_rollout_missing_resource_never_recreates():
    async with coordinator([(200, tokens()), (404, {})]) as (client, wire):
        with pytest.raises(CappUnavailableError) as caught:
            await client.rollout(**rollout_args())
    assert caught.value.__context__ is None
    assert [call[0] for call in wire.calls] == ["POST", "GET"]


async def test_rollout_replaced_uid_is_not_updated():
    async with coordinator([(200, tokens()), (200, resource())]) as (client, wire):
        with pytest.raises(CappUnavailableError):
            await client.rollout(**rollout_args("ffffffff-ffff-ffff-ffff-ffffffffffff"))
    assert not any(call[0] == "PUT" for call in wire.calls)


@pytest.mark.parametrize(
    "failure",
    [
        (403, {"message": IDENTITY[1]}),
        httpx.ConnectError(IDENTITY[1]),
        (200, {"name": IDENTITY[1]}),
    ],
)
async def test_errors_are_mapped_without_credentials_or_exception_context(
    failure, caplog
):
    async with coordinator([(200, tokens()), failure]) as (client, wire):
        with pytest.raises(CappUnavailableError) as caught:
            await client.create_deployment(**create_args())
    exposed = caplog.text + "".join(traceback.format_exception(caught.value))
    assert all(secret not in exposed for secret in IDENTITY)
    assert caught.value.__context__ is None


async def test_readiness_timeout_bounds_a_stalled_network_call():
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class ResourceClient:
        async def create_capp(self, *args):
            from src.bl.capp.responses import parse_capp

            return parse_capp(resource(), "team", NAME)

        async def get_capp(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    client = CappDeploymentCoordinator(ResourceClient(), readiness_timeout_seconds=0.02)
    with pytest.raises(CappUnavailableError) as caught:
        await client.create_deployment(**create_args())
    assert entered.is_set() and cancelled.is_set()
    assert caught.value.__context__ is None


async def test_cancellation_propagates_during_readiness():
    entered = asyncio.Event()

    class ResourceClient:
        async def create_capp(self, *args):
            from src.bl.capp.responses import parse_capp

            return parse_capp(resource(), "team", NAME)

        async def get_capp(self, *args):
            entered.set()
            await asyncio.Event().wait()

    client = CappDeploymentCoordinator(ResourceClient())
    task = asyncio.create_task(client.create_deployment(**create_args()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), None])
def test_invalid_readiness_settings_fail_closed(value):
    with pytest.raises(CappError):
        CappDeploymentCoordinator(None, readiness_timeout_seconds=value)
