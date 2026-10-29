"""D16 wire contracts. No network or DB needed; transport asserts every timeout."""

import copy
import httpx
import json
from http import HTTPStatus

import pytest

from src.bl.capp_client import CappClient
from src.bl.capp_errors import (
    CappConflictError,
    CappError,
    CappErrorCode,
)
from src.bl.capp_models import (
    CappRequest,
    CappWalletTarget,
    ConditionSource,
    ConditionStatus,
    SecretRequest,
    SecretUpdateRequest,
)
from src.bl.cascade_adapters import CappDeletionClient
from src.config import load_capp_client_settings

from tests.capp_fakes import (
    NOW,
    IMAGE,
    NEXT_IMAGE,
    NAME,
    SECRET,
    ROOT,
    PATH,
    IDENTITY,
    Wire,
    tokens,
    capp,
    secret,
)

_clients = []


@pytest.fixture(autouse=True)
async def clean_tables():
    yield
    while _clients:
        await _clients.pop().aclose()


def client(replies):
    wire = Wire(replies)
    adapter = CappClient(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        cluster="dev",
        timeout_seconds=7,
        transport=httpx.MockTransport(wire),
        clock=lambda: NOW,
    )
    _clients.append(adapter)
    return adapter, wire


async def test_create_maps_identity_url_and_configuration():
    adapter, wire = client([(HTTPStatus.OK, tokens()), (HTTPStatus.CREATED, capp())])
    config = {
        "scaleSpec": {"minReplicas": 1},
        "routeSpec": {"hostname": "hello.example"},
        "secretVolumes": [
            {
                "name": "team-data",
                "secretName": "missing-team-secret",
                "mountPath": "/secrets",
            }
        ],
    }
    result = await adapter.create_capp(
        CappWalletTarget("dev", "team"), CappRequest(NAME, IMAGE, config)
    )
    assert (result.uid, result.name, result.deployment_url) == (
        "capp-uid",
        NAME,
        "https://hello.example",
    )
    assert result.conditions[0].status == "True"
    assert wire.calls[0][:3] == (
        "POST",
        ROOT + "/auth/login",
        {"username": IDENTITY[0], "password": IDENTITY[1]},
    )
    assert wire.calls[1][:3] == (
        "POST",
        PATH + "capps",
        {**config, "name": NAME, "image": IMAGE},
    )
    assert wire.calls[1][3]["authorization"] == "Bearer " + IDENTITY[2]
    assert not wire.replies  # No preflight for the optional team Secret.


@pytest.mark.parametrize("ready", ["True", "False", "Unknown"])
async def test_readiness(ready):
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (
                HTTPStatus.OK,
                capp(status={"conditions": [{"type": "Ready", "status": ready}]}),
            ),
        ]
    )
    assert (await adapter.get_capp("team", NAME)).conditions[0].status == ready
    assert wire.calls[1][:2] == ("GET", PATH + "capps/" + NAME)


async def test_rollout_preserves_full_spec_and_does_not_change_input():
    current = capp(
        secretVolumes=[
            {"name": "team-data", "secretName": "team-secret", "mountPath": "/secrets"}
        ],
        env=[{"name": "A", "value": "B"}],
        labels={"server": "owned"},
    )
    original = copy.deepcopy(current)
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.OK, current),
            (HTTPStatus.OK, capp(image=NEXT_IMAGE)),
        ]
    )
    result = await adapter.update_capp(
        "team",
        NAME,
        CappRequest(NAME, NEXT_IMAGE, {"routeSpec": {"routeTimeoutSeconds": 300}}),
    )
    body = wire.calls[2][2]
    assert wire.calls[2][:2] == ("PUT", PATH + "capps/" + NAME)
    assert body["image"] == NEXT_IMAGE
    assert body["secretVolumes"] == current["secretVolumes"]
    assert body["env"] == current["env"]
    assert body["routeSpec"] == {**current["routeSpec"], "routeTimeoutSeconds": 300}
    assert not {"status", "uid", "labels", "resourceVersion"} & body.keys()
    assert current == original and result.image == NEXT_IMAGE


@pytest.mark.parametrize(
    "changes,expected,absent",
    [
        ({}, "customResources", "size"),
        ({"size": "medium"}, "size", "customResources"),
        ({"customResources": {"requests": {"cpu": "2"}}}, "customResources", "size"),
    ],
)
async def test_custom_resources_survive_replacement(changes, expected, absent):
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.OK, capp(size=None, resources={"requests": {"cpu": "1"}})),
            (HTTPStatus.OK, capp()),
        ]
    )
    (await adapter.update_capp("team", NAME, CappRequest(NAME, IMAGE, changes)))
    assert wire.calls[-1][2][expected]
    assert not wire.calls[-1][2].get(absent)


@pytest.mark.parametrize(
    "status,code",
    [
        (HTTPStatus.FORBIDDEN, "authorization"),
        (HTTPStatus.NOT_FOUND, "not_found"),
        (HTTPStatus.CONFLICT, "conflict"),
        (HTTPStatus.SERVICE_UNAVAILABLE, "unavailable"),
    ],
)
async def test_failed_rollout_is_not_retried_or_committed(status, code):
    old = capp()
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.OK, old),
            (status, {"error": IDENTITY[1]}),
        ]
    )
    with pytest.raises(CappError) as caught:
        (await adapter.update_capp("team", NAME, CappRequest(NAME, NEXT_IMAGE)))
    assert caught.value.code == code
    if status == HTTPStatus.CONFLICT:
        assert isinstance(caught.value, CappConflictError)
    assert old["image"] == IMAGE
    assert [c[0] for c in wire.calls] == ["POST", "GET", "PUT"]


async def test_secret_crud_replaces_all_data_and_excludes_values_from_results():
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.CREATED, secret()),
            (HTTPStatus.OK, secret()),
            (HTTPStatus.OK, secret()),
            (HTTPStatus.OK, secret()),
            (HTTPStatus.NO_CONTENT, {}),
        ]
    )
    spec = SecretRequest(SECRET, {"key": "secret-sensitive"})
    result = await adapter.create_secret("team", spec)
    (await adapter.get_secret("team", SECRET))
    (
        await adapter.update_secret(
            "team", SECRET, SecretUpdateRequest({"new": "replacement-sensitive"})
        )
    )
    (await adapter.delete_secret("team", SECRET))
    assert wire.calls[1][:3] == (
        "POST",
        PATH + "secrets",
        {"name": SECRET, "type": "Opaque", "data": spec.data},
    )
    assert wire.calls[4][:3] == (
        "PUT",
        PATH + "secrets/" + SECRET,
        {"data": {"new": "replacement-sensitive"}},
    )
    assert wire.calls[-1][:2] == ("DELETE", PATH + "secrets/" + SECRET)
    assert "secret-sensitive" not in repr(result) + repr(spec)
    assert not hasattr(result, "data")
    assert result.resource_version == "43"


@pytest.mark.parametrize("status", [HTTPStatus.NO_CONTENT, HTTPStatus.NOT_FOUND])
@pytest.mark.parametrize("kind,name", [("capp", NAME), ("secret", SECRET)])
async def test_delete_idempotent(status, kind, name):
    adapter, wire = client([(HTTPStatus.OK, tokens()), (status, {})])
    assert isinstance(adapter, CappDeletionClient)
    assert (await getattr(adapter, "delete_" + kind)(wallet="team", name=name)) is None
    assert wire.calls[-1][:2] == ("DELETE", PATH + kind + "s/" + name)


@pytest.mark.parametrize(
    "operation,prefix",
    [
        (lambda c: c.create_capp("team", CappRequest(NAME, IMAGE)), []),
        (lambda c: c.get_capp("team", NAME), []),
        (
            lambda c: c.update_capp("team", NAME, CappRequest(NAME, NEXT_IMAGE)),
            [(HTTPStatus.OK, capp())],
        ),
        (lambda c: c.delete_capp("team", NAME), []),
        (
            lambda c: c.create_secret(
                "team", SecretRequest(SECRET, {"key": "secret-sensitive"})
            ),
            [],
        ),
        (lambda c: c.get_secret("team", SECRET), []),
        (
            lambda c: c.update_secret("team", SECRET, SecretUpdateRequest({})),
            [(HTTPStatus.OK, secret())],
        ),
        (lambda c: c.delete_secret("team", SECRET), []),
    ],
)
async def test_every_operation_timeout_without_replay(operation, prefix):
    adapter, wire = client(
        [(HTTPStatus.OK, tokens()), *prefix, TimeoutError("password-sensitive")]
    )
    with pytest.raises(CappError) as caught:
        (await operation(adapter))
    assert caught.value.code == "timeout"
    assert len(wire.calls) == len(prefix) + 2
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "target",
    [
        "../team",
        "team/a",
        "team%2fa",
        "team\n",
        "",
        CappWalletTarget("../other", "team"),
        CappWalletTarget("dev", "../team"),
    ],
)
async def test_malformed_targets_fail_before_authentication(target):
    adapter, wire = client([])
    with pytest.raises(CappError, match="invalid_target"):
        (await adapter.get_capp(target, NAME))
    assert not wire.calls


@pytest.mark.parametrize(
    "operation",
    [
        lambda c: c.delete_secret("team", "team-secret"),
        lambda c: c.create_capp("team", CappRequest(NAME, "registry/image:latest")),
        lambda c: c.create_capp(
            "team", CappRequest(NAME, IMAGE, {"namespace": "other"})
        ),
        lambda c: c.create_capp(
            "team", CappRequest(NAME, IMAGE, {"state": "disabled"})
        ),
        lambda c: c.get_capp("team", "../other"),
    ],
)
async def test_invalid_inputs_make_no_calls(operation):
    adapter, wire = client([])
    with pytest.raises(CappError):
        (await operation(adapter))
    assert not wire.calls


@pytest.mark.parametrize(
    "body",
    [
        {},
        capp(name="other"),
        capp(namespace="other"),
        capp(routeSpec={"hostname": "evil/path"}),
        capp(status={"conditions": [{"type": "Ready", "status": "bad"}]}),
    ],
)
async def test_malformed_resource(body):
    adapter, _ = client([(HTTPStatus.OK, tokens()), (HTTPStatus.OK, body)])
    with pytest.raises(CappError, match="malformed_response"):
        (await adapter.get_capp("team", NAME))


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "http://capp.example"},
        {"base_url": "https://user:password@host"},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 0},
        {"password": ""},
        {"cluster": "../dev"},
    ],
)
async def test_invalid_configuration(changes):
    with pytest.raises(CappError):
        CappClient(
            **{
                **dict(
                    base_url="https://capp.example",
                    username="user",
                    password="password",
                    cluster="dev",
                ),
                **changes,
            }
        )


async def test_environment_settings_and_safe_failures():
    with pytest.raises(CappError, match="configuration"):
        load_capp_client_settings({})
    env = dict(
        CAPP_BASE_URL="https://capp.example",
        CAPP_USERNAME=IDENTITY[0],
        CAPP_PASSWORD=IDENTITY[1],
        CAPP_CLUSTER="dev",
    )
    assert load_capp_client_settings(env)["cluster"] == "dev"
    env["CAPP_REQUEST_TIMEOUT_SECONDS"] = IDENTITY[1]
    with pytest.raises(CappError) as caught:
        load_capp_client_settings(env)
    assert IDENTITY[1] not in str(caught.value) and caught.value.__context__ is None


async def test_d18_resolves_uid_and_legacy_name():
    from uuid import UUID
    from src.bl.cascade_adapters import capp_name_for

    automation_id = UUID(NAME.removeprefix("automation-"))
    assert capp_name_for(automation_id, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") == NAME
    assert capp_name_for(automation_id, NAME) == NAME
    assert capp_name_for(automation_id, None) == NAME


async def test_default_d18_client_uses_server_configuration(monkeypatch):
    from fastapi import FastAPI, Request
    from src.api.deps import get_capp_deletion_client

    monkeypatch.setenv("CAPP_BASE_URL", "https://capp.example")
    monkeypatch.setenv("CAPP_USERNAME", IDENTITY[0])
    monkeypatch.setenv("CAPP_PASSWORD", IDENTITY[1])
    monkeypatch.setenv("CAPP_CLUSTER", "dev")
    app = FastAPI()
    request = Request({"type": "http", "app": app})
    adapter = await get_capp_deletion_client(request)
    assert isinstance(adapter, CappClient)
    assert adapter is await get_capp_deletion_client(request)
    try:
        with pytest.raises(CappError, match="invalid_target"):
            await adapter.delete_capp("../invalid", NAME)
    finally:
        await adapter.aclose()


async def test_condition_enums_preserve_wire_values():
    adapter, _ = client([(HTTPStatus.OK, tokens()), (HTTPStatus.OK, capp())])
    condition = (await adapter.get_capp("team", NAME)).conditions[0]
    assert condition.status is ConditionStatus.TRUE
    assert condition.source is ConditionSource.KNATIVE
    assert json.dumps({"status": condition.status, "source": condition.source}) == (
        '{"status": "True", "source": "knative"}'
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": []},
        {"status": {"conditions": {}}},
        {"status": {"conditions": [None]}},
        {"status": {"conditions": [{"type": "Ready", "status": "True", "source": []}]}},
        {"routeSpec": []},
        {"routeSpec": {"hostname": "hello.example", "tlsEnabled": "false"}},
        {"routeSpec": {"hostname": 123}},
        {"resourceVersion": {}},
    ],
)
async def test_malformed_nested_fields_are_sanitized(changes):
    adapter, _ = client([(HTTPStatus.OK, tokens()), (HTTPStatus.OK, capp(**changes))])
    with pytest.raises(CappError) as caught:
        (await adapter.get_capp("team", NAME))
    assert caught.value.code is CappErrorCode.MALFORMED_RESPONSE
    assert caught.value.__context__ is None


async def test_invalid_port_is_rejected_before_network():
    with pytest.raises(CappError) as caught:
        CappClient(
            base_url="https://capp.example:invalid",
            username=IDENTITY[0],
            password=IDENTITY[1],
            cluster="dev",
        )
    assert caught.value.code is CappErrorCode.CONFIGURATION
    assert caught.value.__context__ is None


async def test_app_lifespan_closes_its_client_and_does_not_share_across_apps(
    monkeypatch,
):
    from fastapi import FastAPI, Request
    from src.api.deps import get_capp_deletion_client
    from src.main import lifespan

    from unittest.mock import AsyncMock
    import src.api.deps as dependencies

    clients = []

    def build():
        client = AsyncMock(spec=CappClient)
        clients.append(client)
        return client

    monkeypatch.setattr(dependencies, "build_capp_client", build)
    first_app, second_app = FastAPI(), FastAPI()
    async with lifespan(first_app), lifespan(second_app):
        first = await get_capp_deletion_client(
            Request({"type": "http", "app": first_app})
        )
        second = await get_capp_deletion_client(
            Request({"type": "http", "app": second_app})
        )
        assert first is not second
        first.aclose.assert_not_awaited()
        second.aclose.assert_not_awaited()
    first.aclose.assert_awaited_once()
    second.aclose.assert_awaited_once()
    assert not hasattr(first_app.state, "capp_client")


@pytest.mark.parametrize(
    "target,cluster",
    [
        ("new-wallet", "dev"),
        (CappWalletTarget("other-cluster", "new-wallet"), "other-cluster"),
    ],
)
async def test_caller_supplies_wallet_without_onboarding_allowlist(target, cluster):
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.OK, capp(namespace="new-wallet")),
        ]
    )
    resource = await adapter.get_capp(target, NAME)
    assert resource.uid == "capp-uid"
    assert (
        wire.calls[-1][1]
        == f"{ROOT}/clusters/{cluster}/namespaces/new-wallet/capps/{NAME}"
    )


@pytest.mark.parametrize(
    "config",
    [
        {"routeSpec": {"tlsEnabled": "false"}},
        {"routeSpec": []},
        {"routeSpec": {"routeTimeoutSeconds": True}},
        {"scaleSpec": {"minReplicas": "1"}},
        {"scaleSpec": {"minReplicas": -1}},
        {"scaleSpec": {"maxReplicas": 0}},
        {"scaleSpec": {"metric": "invalid"}},
        {"size": "huge"},
        {"env": [{"name": "KEY", "value": 4}]},
        {
            "env": [
                {
                    "name": "KEY",
                    "value": "secret-sensitive",
                    "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}},
                }
            ]
        },
        {"env": [{"name": "KEY", "valueFrom": {}}]},
        {"env": "invalid"},
        {"secretVolumes": [{"secretName": "team-secret"}]},
        {"customResources": {"requests": {"cpu": 2}}},
        {"logSpec": {"type": "invalid"}},
        {"eventSourcesSpec": {"sources": [{"name": "source"}]}},
        {
            "eventSourcesSpec": {
                "sources": [
                    {
                        "name": "source",
                        "kafkaSourceConfiguration": {
                            "bootstrapServers": [],
                            "topics": ["topic"],
                            "secretRef": "s",
                        },
                    }
                ]
            }
        },
        {"routeSpec": {"hostname": object()}},
        {"scaleSpec": {"minReplicas": float("nan")}},
    ],
)
@pytest.mark.parametrize("update", [False, True])
async def test_invalid_nested_request_is_rejected_before_auth(config, update):
    adapter, wire = client([])
    request = CappRequest(NAME, IMAGE, config)
    with pytest.raises(CappError) as caught:
        if update:
            await adapter.update_capp("team", NAME, request)
        else:
            await adapter.create_capp("team", request)
    assert caught.value.code is CappErrorCode.INVALID_REQUEST
    assert caught.value.__context__ is None
    assert "secret-sensitive" not in str(caught.value)
    assert not wire.calls


async def test_partial_config_preserves_explicit_null_and_empty_lists():
    config = {
        "scaleSpec": {"minReplicas": None},
        "env": [],
        "routeSpec": {"tlsEnabled": False},
    }
    adapter, wire = client([(HTTPStatus.OK, tokens()), (HTTPStatus.CREATED, capp())])
    await adapter.create_capp("team", CappRequest(NAME, IMAGE, config))
    assert wire.calls[-1][2] == {"name": NAME, "image": IMAGE, **config}


async def test_client_context_closes_http_transport():
    class TrackingTransport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True
            await super().aclose()

    wire = TrackingTransport(lambda request: httpx.Response(200, json={}))
    async with CappClient(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        cluster="dev",
        transport=wire,
    ):
        assert not wire.closed
    assert wire.closed


@pytest.mark.parametrize("update", [False, True])
async def test_empty_custom_resources_conflicts_with_size_before_network(update):
    adapter, wire = client([])
    spec = CappRequest(NAME, IMAGE, {"size": "small", "customResources": {}})
    with pytest.raises(CappError) as caught:
        if update:
            await adapter.update_capp("team", NAME, spec)
        else:
            await adapter.create_capp("team", spec)
    assert caught.value.code is CappErrorCode.INVALID_REQUEST
    assert not wire.calls


async def test_empty_custom_resources_replaces_existing_size():
    adapter, wire = client(
        [(HTTPStatus.OK, tokens()), (HTTPStatus.OK, capp()), (HTTPStatus.OK, capp())]
    )
    await adapter.update_capp(
        "team", NAME, CappRequest(NAME, IMAGE, {"customResources": {}})
    )
    assert wire.calls[-1][2]["customResources"] == {}
    assert "size" not in wire.calls[-1][2]


@pytest.mark.parametrize(
    "existing",
    [
        {"env": "wrong format"},
        {"env": [{"name": "KEY", "value": 42}]},
        {"secretVolumes": [{"secretName": "team-secret"}]},
        {"scaleSpec": {"minReplicas": "1"}},
        {"size": None, "resources": {"requests": {"cpu": 2}}},
        {"size": "small", "customResources": {}},
    ],
)
async def test_rollout_rejects_malformed_existing_config_without_put(existing):
    adapter, wire = client(
        [(HTTPStatus.OK, tokens()), (HTTPStatus.OK, capp(**existing))]
    )
    with pytest.raises(CappError) as caught:
        await adapter.update_capp("team", NAME, CappRequest(NAME, NEXT_IMAGE))
    assert caught.value.code is CappErrorCode.MALFORMED_RESPONSE
    assert caught.value.__context__ is None
    assert [call[0] for call in wire.calls] == ["POST", "GET"]


async def test_rollout_checks_configuration_after_merge():
    # Array replacements remain intact while the final request is validated.
    existing = {"eventSourcesSpec": {"sources": []}}
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.OK, capp(**existing)),
            (HTTPStatus.OK, capp(image=NEXT_IMAGE)),
        ]
    )
    config = {
        "eventSourcesSpec": {
            "sources": [
                {"name": "ping", "pingSourceConfiguration": {"schedule": "* * * * *"}}
            ]
        }
    }
    await adapter.update_capp("team", NAME, CappRequest(NAME, NEXT_IMAGE, config))
    assert wire.calls[-1][2]["eventSourcesSpec"] == config["eventSourcesSpec"]
