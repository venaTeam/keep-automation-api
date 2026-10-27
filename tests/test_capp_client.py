"""D16 wire contracts. No network or DB needed; transport asserts every timeout."""

import copy
import json
from http import HTTPStatus
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from src.bl.capp_client import CappClient, _NoRedirect
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


@pytest.fixture(autouse=True)
def clean_tables():
    """Override the suite's DB cleanup: these wire tests never use a database."""
    yield


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


class Response(BytesIO):
    def __init__(self, status, body):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status = status


class Wire:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, request, *, timeout):
        assert timeout == 7
        self.calls.append(
            (
                request.method,
                request.full_url,
                json.loads(request.data) if request.data else None,
                dict(request.header_items()),
            )
        )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Response(*reply)


def client(replies):
    wire = Wire(replies)
    adapter = CappClient(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        wallets={"team": "dev"},
        timeout_seconds=7,
        opener=wire,
        clock=lambda: NOW,
    )
    return adapter, wire


def test_create_maps_identity_url_and_configuration():
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
    result = adapter.create_capp(
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
    assert wire.calls[1][3]["Authorization"] == "Bearer " + IDENTITY[2]
    assert not wire.replies  # No preflight for the optional team Secret.


@pytest.mark.parametrize("ready", ["True", "False", "Unknown"])
def test_readiness(ready):
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (
                HTTPStatus.OK,
                capp(status={"conditions": [{"type": "Ready", "status": ready}]}),
            ),
        ]
    )
    assert adapter.get_capp("team", NAME).conditions[0].status == ready
    assert wire.calls[1][:2] == ("GET", PATH + "capps/" + NAME)


def test_rollout_preserves_full_spec_and_does_not_change_input():
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
    result = adapter.update_capp(
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
def test_custom_resources_survive_replacement(changes, expected, absent):
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.OK, capp(size=None, resources={"requests": {"cpu": "1"}})),
            (HTTPStatus.OK, capp()),
        ]
    )
    adapter.update_capp("team", NAME, CappRequest(NAME, IMAGE, changes))
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
def test_failed_rollout_is_not_retried_or_committed(status, code):
    old = capp()
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.OK, old),
            (status, {"error": IDENTITY[1]}),
        ]
    )
    with pytest.raises(CappError) as caught:
        adapter.update_capp("team", NAME, CappRequest(NAME, NEXT_IMAGE))
    assert caught.value.code == code
    if status == HTTPStatus.CONFLICT:
        assert isinstance(caught.value, CappConflictError)
    assert old["image"] == IMAGE
    assert [c[0] for c in wire.calls] == ["POST", "GET", "PUT"]


def test_secret_crud_replaces_all_data_and_excludes_values_from_results():
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
    result = adapter.create_secret("team", spec)
    adapter.get_secret("team", SECRET)
    adapter.update_secret(
        "team", SECRET, SecretUpdateRequest({"new": "replacement-sensitive"})
    )
    adapter.delete_secret("team", SECRET)
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
def test_delete_idempotent(status, kind, name):
    adapter, wire = client([(HTTPStatus.OK, tokens()), (status, {})])
    assert isinstance(adapter, CappDeletionClient)
    assert getattr(adapter, "delete_" + kind)(wallet="team", name=name) is None
    assert wire.calls[-1][:2] == ("DELETE", PATH + kind + "s/" + name)


def test_401_refresh_retry_once():
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.UNAUTHORIZED, {}),
            (HTTPStatus.OK, tokens("new-access")),
            (HTTPStatus.OK, capp()),
        ]
    )
    adapter.get_capp("team", NAME)
    assert wire.calls[2][:3] == (
        "POST",
        ROOT + "/auth/refresh",
        {"refreshToken": IDENTITY[3]},
    )
    assert wire.calls[3][3]["Authorization"] == "Bearer new-access"


def test_repeated_401_stops():
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.UNAUTHORIZED, {}),
            (HTTPStatus.OK, tokens("new-access")),
            (HTTPStatus.UNAUTHORIZED, {}),
        ]
    )
    with pytest.raises(CappAuthenticationError):
        adapter.get_capp("team", NAME)
    assert len(wire.calls) == 4


@pytest.mark.parametrize(
    "refresh_reply", [(HTTPStatus.UNAUTHORIZED, {}), TimeoutError("refresh-sensitive")]
)
def test_refresh_failure_falls_back_to_one_login(refresh_reply):
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.OK, capp()),
            refresh_reply,
            (HTTPStatus.OK, tokens("new-access")),
            (HTTPStatus.OK, capp()),
        ]
    )
    adapter.get_capp("team", NAME)
    adapter._clock = lambda: NOW + timedelta(minutes=59, seconds=45)
    adapter.get_capp("team", NAME)
    assert [c[1].split("/")[-1] for c in wire.calls] == [
        "login",
        NAME,
        "refresh",
        "login",
        NAME,
    ]


def test_concurrent_first_login_serialized():
    adapter, wire = client([(HTTPStatus.OK, tokens())])
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert (
            list(pool.map(lambda _: adapter._authenticate(), range(32)))
            == [IDENTITY[2]] * 32
        )
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
def test_every_operation_timeout_without_replay(operation, prefix):
    adapter, wire = client(
        [(HTTPStatus.OK, tokens()), *prefix, TimeoutError("password-sensitive")]
    )
    with pytest.raises(CappError) as caught:
        operation(adapter)
    assert caught.value.code == "timeout"
    assert len(wire.calls) == len(prefix) + 2
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("password-sensitive"),
        URLError("access-sensitive"),
        (HTTPStatus.FORBIDDEN, {"error": "password-sensitive secret-sensitive"}),
        (HTTPStatus.OK, b"password-sensitive"),
        HTTPError(
            "https://password-sensitive",
            HTTPStatus.FORBIDDEN,
            "refresh-sensitive",
            {},
            BytesIO(b"secret-sensitive"),
        ),
    ],
)
def test_redaction_across_transport_errors(failure, caplog):
    adapter, wire = client([(HTTPStatus.OK, tokens()), failure])
    request = SecretRequest(SECRET, {"key": "secret-sensitive"})
    with pytest.raises(CappError) as caught:
        adapter.create_secret("team", request)
    exposed = (
        caplog.text
        + repr(caught.value)
        + str(vars(caught.value))
        + "".join(traceback.format_exception(caught.value))
    )
    assert all(value not in exposed for value in (*IDENTITY, "secret-sensitive"))
    assert caught.value.__context__ is None


def test_login_timeout_is_sanitized():
    adapter, wire = client([TimeoutError(IDENTITY[1])])
    with pytest.raises(CappError, match="timeout"):
        adapter.get_capp("team", NAME)
    assert len(wire.calls) == 1


@pytest.mark.parametrize("credential", IDENTITY)
def test_success_body_cannot_reflect_identity_to_callers(credential):
    adapter, _ = client(
        [(HTTPStatus.OK, tokens()), (HTTPStatus.OK, capp(uid="echo:" + credential))]
    )
    with pytest.raises(CappError, match="malformed_response") as caught:
        adapter.get_capp("team", NAME)
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
def test_untrusted_targets_fail_before_authentication(target):
    adapter, wire = client([])
    with pytest.raises(CappError, match="invalid_target"):
        adapter.get_capp(target, NAME)
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
def test_invalid_inputs_make_no_calls(operation):
    adapter, wire = client([])
    with pytest.raises(CappError):
        operation(adapter)
    assert not wire.calls


@pytest.mark.parametrize(
    "body", [{}, {"accessToken": "bad\ntoken"}, {**tokens(), "expiresAt": "invalid"}]
)
def test_malformed_auth(body):
    adapter, _ = client([(HTTPStatus.OK, body)])
    with pytest.raises(CappError, match="malformed_response"):
        adapter.get_capp("team", NAME)


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
def test_malformed_resource(body):
    adapter, _ = client([(HTTPStatus.OK, tokens()), (HTTPStatus.OK, body)])
    with pytest.raises(CappError, match="malformed_response"):
        adapter.get_capp("team", NAME)


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
def test_invalid_configuration(changes):
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


def test_environment_settings_and_safe_failures():
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


def test_no_redirects():
    assert (
        _NoRedirect().redirect_request(
            None, None, HTTPStatus.TEMPORARY_REDIRECT, "", {}, "https://other"
        )
        is None
    )


def test_d18_resolves_uid_and_legacy_name():
    from uuid import UUID
    from src.bl.cascade_adapters import capp_name_for

    automation_id = UUID(NAME.removeprefix("automation-"))
    assert capp_name_for(automation_id, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") == NAME
    assert capp_name_for(automation_id, NAME) == NAME
    assert capp_name_for(automation_id, None) == NAME


def test_default_d18_client_uses_server_configuration(monkeypatch):
    from src.bl import cascade_adapters

    monkeypatch.setenv("CAPP_BASE_URL", "https://capp.example")
    monkeypatch.setenv("CAPP_USERNAME", IDENTITY[0])
    monkeypatch.setenv("CAPP_PASSWORD", IDENTITY[1])
    monkeypatch.setenv("CAPP_ONBOARDED_WALLETS", '{"team":"dev"}')
    monkeypatch.setattr(cascade_adapters, "_default_capp", None)
    adapter = cascade_adapters.get_default_capp_deletion_client()
    assert isinstance(adapter, CappClient)
    assert adapter is cascade_adapters.get_default_capp_deletion_client()
    with pytest.raises(CappError, match="invalid_target"):
        adapter.delete_capp("unregistered", NAME)


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
def test_http_failures_have_consistent_typed_codes(
    status, code, retryable, during_login
):
    replies = [] if during_login else [(HTTPStatus.OK, tokens())]
    adapter, wire = client([*replies, (status, {"error": IDENTITY[1]})])
    with pytest.raises(CappError) as caught:
        adapter.get_capp("team", NAME)
    assert caught.value.code is code
    assert caught.value.http_status == status
    assert caught.value.retryable is retryable
    assert len(wire.calls) == len(replies) + 1


def test_condition_enums_preserve_wire_values():
    adapter, _ = client([(HTTPStatus.OK, tokens()), (HTTPStatus.OK, capp())])
    condition = adapter.get_capp("team", NAME).conditions[0]
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
def test_malformed_nested_fields_are_sanitized(changes):
    adapter, _ = client([(HTTPStatus.OK, tokens()), (HTTPStatus.OK, capp(**changes))])
    with pytest.raises(CappError) as caught:
        adapter.get_capp("team", NAME)
    assert caught.value.code is CappErrorCode.MALFORMED_RESPONSE
    assert caught.value.__context__ is None


def test_invalid_port_is_rejected_before_network():
    with pytest.raises(CappError) as caught:
        CappClient(
            base_url="https://capp.example:invalid",
            username=IDENTITY[0],
            password=IDENTITY[1],
            wallets={},
        )
    assert caught.value.code is CappErrorCode.CONFIGURATION
    assert caught.value.__context__ is None
