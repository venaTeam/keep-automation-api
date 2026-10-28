"""D16 wire contracts. No network or DB needed; transport asserts every timeout."""

import asyncio
import copy
import httpx
import json
from http import HTTPStatus
import traceback
from datetime import datetime, timedelta, timezone

import pytest

from src.bl.capp_client import CappClient
from src.bl.capp_errors import (
    CappAuthenticationError,
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

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
IMAGE = "registry.example/automation@sha256:" + "a" * 64
NEXT_IMAGE = "registry.example/automation@sha256:" + "b" * 64
NAME = "automation-12345678-1234-1234-1234-123456789012"
SECRET = NAME + "-run-auth"
ROOT = "https://capp.example/api/v1"
PATH = ROOT + "/clusters/dev/namespaces/team/"
IDENTITY = (
    "service-user-sensitive",
    "password-sensitive",
    "access-sensitive",
    "refresh-sensitive",
)


_clients = []


@pytest.fixture(autouse=True)
async def clean_tables():
    """Override the suite's DB cleanup: these wire tests never use a database."""
    yield
    while _clients:
        await _clients.pop().aclose()


def tokens(access=IDENTITY[2], expires=None):
    return {
        "accessToken": access,
        "refreshToken": IDENTITY[3],
        "expiresAt": (expires or NOW + timedelta(hours=1)).isoformat(),
    }


def capp(**changes):
    return {
        "name": NAME,
        "namespace": "team",
        "uid": "capp-uid",
        "image": IMAGE,
        "resourceVersion": "42",
        "size": "small",
        "state": "enabled",
        "routeSpec": {
            "hostname": "hello.example",
            "tlsEnabled": True,
            "routeTimeoutSeconds": 600,
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True", "source": "knative"}]
        },
        **changes,
    }


def secret():
    return {
        "name": SECRET,
        "namespace": "team",
        "uid": "secret-uid",
        "resourceVersion": "43",
        "data": {"old": "secret-sensitive"},
    }


class Wire:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def __call__(self, request):
        assert request.extensions["timeout"] == dict(connect=7, read=7, write=7, pool=7)
        self.calls.append(
            (
                request.method,
                str(request.url),
                json.loads(request.content) if request.content else None,
                dict(request.headers),
            )
        )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)


def client(replies):
    wire = Wire(replies)
    adapter = CappClient(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        wallets={"team": "dev"},
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
        secretVolumes=[{"secretName": "team-secret"}],
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


async def test_401_refresh_retry_once():
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.UNAUTHORIZED, {}),
            (HTTPStatus.OK, tokens("new-access")),
            (HTTPStatus.OK, capp()),
        ]
    )
    (await adapter.get_capp("team", NAME))
    assert wire.calls[2][:3] == (
        "POST",
        ROOT + "/auth/refresh",
        {"refreshToken": IDENTITY[3]},
    )
    assert wire.calls[3][3]["authorization"] == "Bearer new-access"


async def test_repeated_401_stops():
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.UNAUTHORIZED, {}),
            (HTTPStatus.OK, tokens("new-access")),
            (HTTPStatus.UNAUTHORIZED, {}),
        ]
    )
    with pytest.raises(CappAuthenticationError):
        (await adapter.get_capp("team", NAME))
    assert len(wire.calls) == 4


@pytest.mark.parametrize(
    "refresh_reply", [(HTTPStatus.UNAUTHORIZED, {}), TimeoutError("refresh-sensitive")]
)
async def test_refresh_failure_falls_back_to_one_login(refresh_reply):
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.OK, capp()),
            refresh_reply,
            (HTTPStatus.OK, tokens("new-access")),
            (HTTPStatus.OK, capp()),
        ]
    )
    (await adapter.get_capp("team", NAME))
    adapter._transport._clock = lambda: NOW + timedelta(minutes=59, seconds=45)
    (await adapter.get_capp("team", NAME))
    assert [c[1].split("/")[-1] for c in wire.calls] == [
        "login",
        NAME,
        "refresh",
        "login",
        NAME,
    ]


async def test_concurrent_first_login_serialized():
    adapter, wire = client([(HTTPStatus.OK, tokens())])
    results = await asyncio.gather(
        *(adapter._transport._authenticate() for _ in range(32))
    )
    assert results == [IDENTITY[2]] * 32
    assert len(wire.calls) == 1


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
    "failure",
    [
        TimeoutError("password-sensitive"),
        httpx.ConnectError("access-sensitive"),
        (HTTPStatus.FORBIDDEN, {"error": "password-sensitive secret-sensitive"}),
        (HTTPStatus.OK, b"password-sensitive"),
        httpx.RemoteProtocolError("refresh-sensitive secret-sensitive"),
    ],
)
async def test_redaction_across_transport_errors(failure, caplog):
    adapter, wire = client([(HTTPStatus.OK, tokens()), failure])
    request = SecretRequest(SECRET, {"key": "secret-sensitive"})
    with pytest.raises(CappError) as caught:
        (await adapter.create_secret("team", request))
    exposed = (
        caplog.text
        + repr(caught.value)
        + str(vars(caught.value))
        + "".join(traceback.format_exception(caught.value))
    )
    assert all(value not in exposed for value in (*IDENTITY, "secret-sensitive"))
    assert caught.value.__context__ is None


async def test_login_timeout_is_sanitized():
    adapter, wire = client([TimeoutError(IDENTITY[1])])
    with pytest.raises(CappError, match="timeout"):
        (await adapter.get_capp("team", NAME))
    assert len(wire.calls) == 1


@pytest.mark.parametrize("credential", IDENTITY)
async def test_success_body_cannot_reflect_identity_to_callers(credential):
    adapter, _ = client(
        [(HTTPStatus.OK, tokens()), (HTTPStatus.OK, capp(uid="echo:" + credential))]
    )
    with pytest.raises(CappError, match="malformed_response") as caught:
        (await adapter.get_capp("team", NAME))
    assert credential not in str(caught.value)


@pytest.mark.parametrize(
    "target",
    [
        "unknown",
        "../team",
        "team/a",
        "team%2fa",
        "team\n",
        "",
        CappWalletTarget("other", "team"),
    ],
)
async def test_untrusted_targets_fail_before_authentication(target):
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
    "body", [{}, {"accessToken": "bad\ntoken"}, {**tokens(), "expiresAt": "invalid"}]
)
async def test_malformed_auth(body):
    adapter, _ = client([(HTTPStatus.OK, body)])
    with pytest.raises(CappError, match="malformed_response"):
        (await adapter.get_capp("team", NAME))


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
        {"wallets": []},
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
                    wallets={},
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
        CAPP_ONBOARDED_WALLETS='{"team":"dev"}',
    )
    assert load_capp_client_settings(env)["wallets"] == {"team": "dev"}
    env["CAPP_REQUEST_TIMEOUT_SECONDS"] = IDENTITY[1]
    with pytest.raises(CappError) as caught:
        load_capp_client_settings(env)
    assert IDENTITY[1] not in str(caught.value) and caught.value.__context__ is None


async def test_no_redirects():
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            HTTPStatus.TEMPORARY_REDIRECT, headers={"Location": "https://other"}
        )

    async with CappClient(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        wallets={"team": "dev"},
        transport=httpx.MockTransport(handler),
    ) as adapter:
        with pytest.raises(CappError):
            await adapter.get_capp("team", NAME)
    assert calls == [ROOT + "/auth/login"]


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
    monkeypatch.setenv("CAPP_ONBOARDED_WALLETS", '{"team":"dev"}')
    app = FastAPI()
    request = Request({"type": "http", "app": app})
    adapter = await get_capp_deletion_client(request)
    assert isinstance(adapter, CappClient)
    assert adapter is await get_capp_deletion_client(request)
    try:
        with pytest.raises(CappError, match="invalid_target"):
            await adapter.delete_capp("unregistered", NAME)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (HTTPStatus.BAD_REQUEST, CappErrorCode.INVALID_REQUEST, False),
        (HTTPStatus.FORBIDDEN, CappErrorCode.AUTHORIZATION, False),
        (HTTPStatus.REQUEST_TIMEOUT, CappErrorCode.TIMEOUT, True),
        (HTTPStatus.TOO_MANY_REQUESTS, CappErrorCode.UNAVAILABLE, True),
        (HTTPStatus.SERVICE_UNAVAILABLE, CappErrorCode.UNAVAILABLE, True),
        (599, CappErrorCode.UNAVAILABLE, True),  # Nonstandard proxy status.
    ],
)
@pytest.mark.parametrize("during_login", [True, False])
async def test_http_failures_have_consistent_typed_codes(
    status, code, retryable, during_login
):
    replies = [] if during_login else [(HTTPStatus.OK, tokens())]
    adapter, wire = client([*replies, (status, {"error": IDENTITY[1]})])
    with pytest.raises(CappError) as caught:
        (await adapter.get_capp("team", NAME))
    assert caught.value.code is code
    assert caught.value.http_status == status
    assert caught.value.retryable is retryable
    assert len(wire.calls) == len(replies) + 1


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
            wallets={},
        )
    assert caught.value.code is CappErrorCode.CONFIGURATION
    assert caught.value.__context__ is None


async def test_slow_requests_overlap_and_login_happens_once():
    active = 0
    maximum = 0
    login_calls = 0
    both_started = asyncio.Event()

    async def handler(request):
        nonlocal active, maximum, login_calls
        if request.url.path.endswith("/login"):
            login_calls += 1
            await asyncio.sleep(0)
            return httpx.Response(HTTPStatus.OK, json=tokens())
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        active -= 1
        return httpx.Response(HTTPStatus.OK, json=capp())

    async with CappClient(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        wallets={"team": "dev"},
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as adapter:
        await asyncio.gather(
            adapter.get_capp("team", NAME), adapter.get_capp("team", NAME)
        )
    assert maximum == 2 and login_calls == 1
    assert adapter._transport._http.is_closed


async def test_concurrent_401_responses_share_one_refresh():
    first_requests = 0
    refreshes = 0
    both_failed = asyncio.Event()

    async def handler(request):
        nonlocal first_requests, refreshes
        if request.url.path.endswith("/login"):
            return httpx.Response(HTTPStatus.OK, json=tokens())
        if request.url.path.endswith("/refresh"):
            refreshes += 1
            await asyncio.sleep(0)
            return httpx.Response(HTTPStatus.OK, json=tokens("new-access"))
        if request.headers["Authorization"] == "Bearer " + IDENTITY[2]:
            first_requests += 1
            if first_requests == 2:
                both_failed.set()
            await asyncio.wait_for(both_failed.wait(), timeout=1)
            return httpx.Response(HTTPStatus.UNAUTHORIZED)
        return httpx.Response(HTTPStatus.OK, json=capp())

    async with CappClient(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        wallets={"team": "dev"},
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as adapter:
        await asyncio.gather(
            adapter.get_capp("team", NAME), adapter.get_capp("team", NAME)
        )
    assert first_requests == 2 and refreshes == 1


async def test_total_deadline_bounds_slow_stream_and_closes_response():
    class SlowStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"{"
            await asyncio.sleep(10)

        async def aclose(self):
            self.closed = True

    stream = SlowStream()
    mutations = 0

    async def handler(request):
        nonlocal mutations
        if request.url.path.endswith("/login"):
            return httpx.Response(HTTPStatus.OK, json=tokens())
        mutations += 1
        return httpx.Response(HTTPStatus.CREATED, stream=stream)

    async with CappClient(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        wallets={"team": "dev"},
        timeout_seconds=0.05,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as adapter:
        with pytest.raises(CappError) as caught:
            await adapter.create_capp("team", CappRequest(NAME, IMAGE))
        assert caught.value.code is CappErrorCode.TIMEOUT
        assert caught.value.__context__ is None
    assert stream.closed and mutations == 1


async def test_cancelled_login_releases_auth_lock():
    started = asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()
        return httpx.Response(HTTPStatus.OK, json=tokens())

    async with CappClient(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        wallets={"team": "dev"},
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as adapter:
        task = asyncio.create_task(adapter._transport._authenticate())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not adapter._transport._auth_lock.locked()
        assert (
            await asyncio.wait_for(adapter._transport._authenticate(), timeout=1)
            == IDENTITY[2]
        )
    assert calls == 2


async def test_app_lifespan_closes_its_client_and_does_not_share_across_apps(
    monkeypatch,
):
    from fastapi import FastAPI, Request
    from src.api.deps import get_capp_deletion_client
    from src.main import lifespan

    for name, value in {
        "CAPP_BASE_URL": "https://capp.example",
        "CAPP_USERNAME": IDENTITY[0],
        "CAPP_PASSWORD": IDENTITY[1],
        "CAPP_ONBOARDED_WALLETS": '{"team":"dev"}',
    }.items():
        monkeypatch.setenv(name, value)
    first_app, second_app = FastAPI(), FastAPI()
    async with lifespan(first_app), lifespan(second_app):
        first = await get_capp_deletion_client(
            Request({"type": "http", "app": first_app})
        )
        second = await get_capp_deletion_client(
            Request({"type": "http", "app": second_app})
        )
        assert first is not second
        assert (
            not first._transport._http.is_closed
            and not second._transport._http.is_closed
        )
    assert first._transport._http.is_closed and second._transport._http.is_closed
    assert not hasattr(first_app.state, "capp_client")
