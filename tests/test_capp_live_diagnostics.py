"""Offline checks for the opt-in hello runner's sanitized diagnostics."""

import traceback
from types import SimpleNamespace

import pytest

from src.bl.capp.errors import CappError, CappErrorCode
from tests import test_capp_live as live


@pytest.fixture(autouse=True)
def clean_tables():
    yield


@pytest.mark.parametrize(
    "failed_stage",
    [
        "configure_client",
        "create_secret",
        "create_capp",
        "readiness",
        "validate_route",
        "invoke",
        "delete_capp",
        "delete_secret",
        "close_client",
    ],
)
async def test_hello_failure_reports_stage_without_sensitive_details(
    monkeypatch, failed_stage
):
    calls = []
    for key, value in {
        "CAPP_HELLO_WALLET": "team",
        "CAPP_HELLO_IMAGE": "registry/hello@sha256:" + "a" * 64,
        "CAPP_HELLO_HOSTNAME": "hello.example",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(live, "load_capp_client_settings", lambda: {})

    def fail(stage):
        calls.append(stage)
        if stage == failed_stage:
            if stage in {"create_capp", "delete_capp", "delete_secret"}:
                raise CappError(CappErrorCode.AUTHORIZATION)
            raise RuntimeError("password-sensitive token-sensitive")

    class Adapter:
        def __init__(self, **kwargs):
            fail("configure_client")

        async def create_secret(self, *args):
            fail("create_secret")

        async def create_capp(self, *args):
            fail("create_capp")

        async def get_capp(self, *args):
            fail("readiness")
            return SimpleNamespace(
                image="registry/hello@sha256:" + "a" * 64,
                conditions=[SimpleNamespace(type="Ready", status="True")],
                deployment_url=(
                    "https://wrong.example"
                    if failed_stage == "validate_route"
                    else "https://hello.example"
                ),
            )

        async def delete_capp(self, *args):
            fail("delete_capp")

        async def delete_secret(self, *args):
            fail("delete_secret")

        async def aclose(self):
            fail("close_client")

    class Invocation:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            fail("invoke")
            return SimpleNamespace(status_code=200, json=lambda: {"status": "ok"})

    monkeypatch.setattr(live, "CappClient", Adapter)
    monkeypatch.setattr(live.httpx, "AsyncClient", Invocation)
    with pytest.raises(pytest.fail.Exception) as caught:
        await live._hello_round_trip()
    message = str(caught.value)
    expected_code = (
        "authorization"
        if failed_stage in {"create_capp", "delete_capp", "delete_secret"}
        else (
            "malformed_response"
            if failed_stage == "validate_route"
            else "unexpected_error"
        )
    )
    assert f"{failed_stage}: {expected_code}" in message
    assert caught.value.__context__ is None
    assert "password-sensitive" not in "".join(traceback.format_exception(caught.value))
    assert "token-sensitive" not in message
    if failed_stage == "configure_client":
        assert calls == ["configure_client"]
    else:
        assert calls[-1] == "close_client"
        if failed_stage == "delete_capp":
            assert "delete_secret" not in calls
            assert "Secret cleanup skipped" in message
            assert "-run-auth" in message
        else:
            assert calls.index("delete_capp") < calls.index("delete_secret")
