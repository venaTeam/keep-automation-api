"""Transport behavior through its public request/close interface."""

import asyncio
import traceback
from contextlib import asynccontextmanager
from datetime import timedelta
from http import HTTPMethod, HTTPStatus
import httpx
import pytest
from src.bl.capp_transport import CappTransport
from src.bl.capp_errors import CappError, CappErrorCode, CappAuthenticationError
from src.bl.capp_models import SecretRequest
from tests.capp_fakes import NOW, NAME, SECRET, ROOT, IDENTITY, Wire, tokens, capp

_clients = []


@pytest.fixture(autouse=True)
async def clean_tables():
    yield
    while _clients:
        await _clients.pop().aclose()


def client(replies, clock=lambda: NOW):
    wire = Wire(replies)
    adapter = CappTransport(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        timeout_seconds=7,
        transport=httpx.MockTransport(wire),
        clock=clock,
    )
    _clients.append(adapter)
    return adapter, wire


@asynccontextmanager
async def transport_client(**kwargs):
    adapter = CappTransport(**kwargs)
    try:
        yield adapter
    finally:
        await adapter.aclose()


async def test_401_refresh_retry_once():
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.UNAUTHORIZED, {}),
            (HTTPStatus.OK, tokens("new-access")),
            (HTTPStatus.OK, capp()),
        ]
    )
    (await adapter.request(HTTPMethod.GET, "/" + NAME))
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
        (await adapter.request(HTTPMethod.GET, "/" + NAME))
    assert len(wire.calls) == 4


@pytest.mark.parametrize(
    "refresh_reply", [(HTTPStatus.UNAUTHORIZED, {}), TimeoutError("refresh-sensitive")]
)
async def test_refresh_failure_falls_back_to_one_login(refresh_reply):
    now = [NOW]
    adapter, wire = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.OK, capp()),
            refresh_reply,
            (HTTPStatus.OK, tokens("new-access")),
            (HTTPStatus.OK, capp()),
        ],
        clock=lambda: now[0],
    )
    (await adapter.request(HTTPMethod.GET, "/" + NAME))
    now[0] = NOW + timedelta(minutes=59, seconds=45)
    (await adapter.request(HTTPMethod.GET, "/" + NAME))
    assert [c[1].split("/")[-1] for c in wire.calls] == [
        "login",
        NAME,
        "refresh",
        "login",
        NAME,
    ]


async def test_concurrent_first_login_serialized():
    adapter, wire = client([(HTTPStatus.OK, tokens()), *[(HTTPStatus.OK, {})] * 32])
    results = await asyncio.gather(
        *(adapter.request(HTTPMethod.GET, "/resource") for _ in range(32))
    )
    assert results == [{}] * 32
    assert sum(call[1].endswith("/login") for call in wire.calls) == 1


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
        (await adapter.request(HTTPMethod.POST, "/secrets", {"data": request.data}))
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
        (await adapter.request(HTTPMethod.GET, "/" + NAME))
    assert sum(call[1].endswith("/login") for call in wire.calls) == 1


@pytest.mark.parametrize("credential", IDENTITY)
async def test_success_body_cannot_reflect_identity_to_callers(credential):
    adapter, _ = client(
        [(HTTPStatus.OK, tokens()), (HTTPStatus.OK, capp(uid="echo:" + credential))]
    )
    with pytest.raises(CappError, match="malformed_response") as caught:
        (await adapter.request(HTTPMethod.GET, "/" + NAME))
    assert credential not in str(caught.value)


@pytest.mark.parametrize(
    "body", [{}, {"accessToken": "bad\ntoken"}, {**tokens(), "expiresAt": "invalid"}]
)
async def test_malformed_auth(body):
    adapter, _ = client([(HTTPStatus.OK, body)])
    with pytest.raises(CappError, match="malformed_response"):
        (await adapter.request(HTTPMethod.GET, "/" + NAME))


async def test_no_redirects():
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            HTTPStatus.TEMPORARY_REDIRECT, headers={"Location": "https://other"}
        )

    async with transport_client(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        transport=httpx.MockTransport(handler),
    ) as adapter:
        with pytest.raises(CappError):
            await adapter.request(HTTPMethod.GET, "/" + NAME)
    assert calls == [ROOT + "/auth/login"]


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
        (await adapter.request(HTTPMethod.GET, "/" + NAME))
    assert caught.value.code is code
    assert caught.value.http_status == status
    assert caught.value.retryable is retryable
    assert len(wire.calls) == len(replies) + 1


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

    async with transport_client(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as adapter:
        await asyncio.gather(
            adapter.request(HTTPMethod.GET, "/" + NAME),
            adapter.request(HTTPMethod.GET, "/" + NAME),
        )
    assert maximum == 2 and login_calls == 1


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

    async with transport_client(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as adapter:
        await asyncio.gather(
            adapter.request(HTTPMethod.GET, "/" + NAME),
            adapter.request(HTTPMethod.GET, "/" + NAME),
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

    async with transport_client(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        timeout_seconds=0.05,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as adapter:
        with pytest.raises(CappError) as caught:
            await adapter.request(HTTPMethod.POST, "/capps", {"name": NAME})
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
        return httpx.Response(
            HTTPStatus.OK, json=tokens() if request.url.path.endswith("/login") else {}
        )

    async with transport_client(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as adapter:
        task = asyncio.create_task(adapter.request(HTTPMethod.GET, "/resource"))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (
            await asyncio.wait_for(
                adapter.request(HTTPMethod.GET, "/resource"), timeout=1
            )
            == {}
        )
    assert calls == 3


@pytest.mark.parametrize(
    "body", [{"bad": object()}, {"bad": float("nan")}, {"bad": float("inf")}]
)
async def test_invalid_request_serialization_is_sanitized_before_auth(body):
    adapter, wire = client([])
    with pytest.raises(CappError) as caught:
        await adapter.request(HTTPMethod.POST, "/capps", body)
    assert caught.value.code is CappErrorCode.INVALID_REQUEST
    assert caught.value.__context__ is None
    assert not wire.calls


async def test_aclose_releases_injected_http_transport():
    class TrackingTransport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True
            await super().aclose()

    wire = TrackingTransport(lambda request: httpx.Response(200, json={}))
    adapter = CappTransport(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        transport=wire,
    )
    assert not wire.closed
    await adapter.aclose()
    assert wire.closed


@pytest.mark.parametrize("credential", IDENTITY[2:])
@pytest.mark.parametrize("in_key", [False, True])
async def test_inflight_response_rejects_tokens_rotated_by_another_request(
    credential, in_key, caplog
):
    old_request_started = asyncio.Event()
    rotated_request_finished = asyncio.Event()
    echoed = {credential: "value"} if in_key else {"nested": [{"value": credential}]}

    async def handler(request):
        path = request.url.path
        if path.endswith("/login"):
            return httpx.Response(HTTPStatus.OK, json=tokens())
        if path.endswith("/refresh"):
            return httpx.Response(
                HTTPStatus.OK,
                json={**tokens("rotated-access"), "refreshToken": "rotated-refresh"},
            )
        if path == "/old":
            assert request.headers["Authorization"] == "Bearer " + IDENTITY[2]
            old_request_started.set()
            await asyncio.wait_for(rotated_request_finished.wait(), timeout=1)
            return httpx.Response(HTTPStatus.OK, json=echoed)
        if request.headers["Authorization"] == "Bearer " + IDENTITY[2]:
            return httpx.Response(HTTPStatus.UNAUTHORIZED)
        return httpx.Response(HTTPStatus.OK, json={"ok": True})

    async with transport_client(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as adapter:

        async def rotate():
            await asyncio.wait_for(old_request_started.wait(), timeout=1)
            try:
                return await adapter.request(HTTPMethod.GET, "/rotate")
            finally:
                rotated_request_finished.set()

        results = await asyncio.gather(
            adapter.request(HTTPMethod.GET, "/old"), rotate(), return_exceptions=True
        )
    error, success = results
    assert isinstance(error, CappError)
    assert error.code is CappErrorCode.MALFORMED_RESPONSE
    assert error.__context__ is None
    assert success == {"ok": True}
    exposed = (
        caplog.text + str(vars(error)) + "".join(traceback.format_exception(error))
    )
    assert credential not in exposed


@pytest.mark.parametrize("credential", IDENTITY[2:])
async def test_401_retry_still_rejects_original_tokens(credential):
    adapter, _ = client(
        [
            (HTTPStatus.OK, tokens()),
            (HTTPStatus.UNAUTHORIZED, {}),
            (
                HTTPStatus.OK,
                {**tokens("rotated-access"), "refreshToken": "rotated-refresh"},
            ),
            (HTTPStatus.OK, {"echo": credential}),
        ]
    )
    with pytest.raises(CappError) as caught:
        await adapter.request(HTTPMethod.GET, "/resource")
    assert caught.value.code is CappErrorCode.MALFORMED_RESPONSE
    assert caught.value.__context__ is None


@pytest.mark.parametrize("depth", [600, None])
@pytest.mark.parametrize("during_login", [False, True])
async def test_deep_response_is_sanitized(depth, during_login, caplog):
    import sys

    depth = depth or sys.getrecursionlimit() * 2
    raw = b'{"nested":' + b"[" * depth + b'"access-sensitive"' + b"]" * depth + b"}"
    prefix = [] if during_login else [(HTTPStatus.OK, tokens())]
    adapter, wire = client([*prefix, (HTTPStatus.OK, raw)])
    with pytest.raises(CappError) as caught:
        await adapter.request(HTTPMethod.GET, "/resource")
    assert caught.value.code is CappErrorCode.MALFORMED_RESPONSE
    assert caught.value.__context__ is None
    assert len(wire.calls) == len(prefix) + 1
    exposed = caplog.text + "".join(traceback.format_exception(caught.value))
    assert all(credential not in exposed for credential in IDENTITY)


async def test_request_body_snapshot_is_preserved_across_authentication_and_retry():
    body = {"value": "original"}
    sent = []

    async def handler(request):
        if request.url.path.endswith("/login"):
            body["value"] = "changed-during-login"
            return httpx.Response(HTTPStatus.OK, json=tokens())
        if request.url.path.endswith("/refresh"):
            body["value"] = "changed-during-refresh"
            return httpx.Response(HTTPStatus.OK, json=tokens("new-access"))
        sent.append(request.content)
        if len(sent) == 1:
            return httpx.Response(HTTPStatus.UNAUTHORIZED)
        return httpx.Response(HTTPStatus.CREATED, json={"ok": True})

    async with transport_client(
        base_url="https://capp.example",
        username=IDENTITY[0],
        password=IDENTITY[1],
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as adapter:
        result = await adapter.request(HTTPMethod.POST, "/resource", body)
    assert result == {"ok": True}
    assert sent == [b'{"value": "original"}', b'{"value": "original"}']
