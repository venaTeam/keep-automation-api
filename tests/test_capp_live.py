"""Opt-in dev CAPP acceptance; never runs from ordinary CI configuration.

The supplied hello image must read /etc/automation/run-auth/api-key and expose
the wrapper's X-API-Key protected POST /run. This is F25-style orchestration,
deliberately outside the management client and the D17 submit retry loop.
"""

import asyncio
import httpx
from http import HTTPStatus
import os
import secrets
import time
from uuid import uuid4

import pytest

from src.bl.capp.client import CappClient
from src.bl.capp.errors import CappError, CappErrorCode, error_for_status
from src.bl.capp.models import CappRequest, SecretRequest
from src.config import load_capp_client_settings


@pytest.fixture(autouse=True)
def clean_tables():
    """This external acceptance test must not acquire a pooled DB connection."""
    yield


@pytest.mark.skipif(
    os.environ.get("CAPP_LIVE_TEST") != "1", reason="Dev CAPP acceptance is opt-in"
)
async def test_dev_hello_round_trip():
    await _hello_round_trip()


def _failure_code(error: Exception) -> str:
    if isinstance(error, CappError):
        return error.code.value
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, httpx.RequestError):
        return "unavailable"
    if isinstance(error, (ValueError, TypeError)):
        return "malformed_response"
    return "unexpected_error"


async def _hello_round_trip():
    wallet = os.environ["CAPP_HELLO_WALLET"]
    image = os.environ["CAPP_HELLO_IMAGE"]
    hostname = os.environ["CAPP_HELLO_HOSTNAME"]
    name = f"automation-{uuid4()}"
    secret_name = name + "-run-auth"
    key = secrets.token_urlsafe(32)
    request = CappRequest(
        name,
        image,
        {
            "state": "enabled",
            "size": "small",
            "scaleSpec": {
                "minReplicas": 1,
                "metric": "concurrency",
                "scaleDelaySeconds": 30,
            },
            "routeSpec": {
                "hostname": hostname,
                "tlsEnabled": True,
                "routeTimeoutSeconds": 30,
            },
            "secretVolumes": [
                {
                    "name": "automation-run-auth",
                    "secretName": secret_name,
                    "mountPath": "/etc/automation/run-auth",
                }
            ],
        },
    )
    adapter = None
    failures = []
    stage = "configure_client"
    try:
        adapter = CappClient(**load_capp_client_settings())
        stage = "create_secret"
        await adapter.create_secret(
            wallet, SecretRequest(secret_name, {"api-key": key})
        )
        stage = "create_capp"
        await adapter.create_capp(wallet, request)
        stage = "readiness"
        deadline = time.monotonic() + 600
        while True:
            resource = await adapter.get_capp(wallet, name)
            ready = [c for c in resource.conditions if c.type == "Ready"]
            if (
                resource.image == image
                and ready
                and all(c.status == "True" for c in ready)
            ):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError()
            await asyncio.sleep(5)
        stage = "validate_route"
        if resource.deployment_url != "https://" + hostname:
            raise CappError(CappErrorCode.MALFORMED_RESPONSE)
        # Service identity stays in CappClient. Invocation uses only this
        # disposable deployment's run key; no retries and no raw output.
        stage = "invoke"
        async with httpx.AsyncClient(
            follow_redirects=False, trust_env=False
        ) as invocation:
            async with asyncio.timeout(30):
                response = await invocation.post(
                    resource.deployment_url + "/run",
                    timeout=30,
                    json={"message": "D16 hello verification"},
                    headers={
                        "X-API-Key": key,
                        "X-Run-Id": str(uuid4()),
                        "X-History-Id": "d16-hello",
                        "X-Attempt": "1",
                    },
                )
                if response.status_code != HTTPStatus.OK:
                    raise error_for_status(response.status_code)
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("status") != "ok":
                    raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    except Exception as exc:
        failures.append(f"{stage}: {_failure_code(exc)}")
    finally:
        if adapter is not None:
            try:
                await adapter.delete_capp(wallet, name)
            except Exception as exc:
                failures.append(
                    f"delete_capp: {_failure_code(exc)}; inspect {name} and {secret_name}; Secret cleanup skipped"
                )
            else:
                try:
                    await adapter.delete_secret(wallet, secret_name)
                except Exception as exc:
                    failures.append(
                        f"delete_secret: {_failure_code(exc)}; inspect {secret_name}"
                    )
            finally:
                try:
                    await adapter.aclose()
                except Exception as exc:
                    failures.append(f"close_client: {_failure_code(exc)}")
    # Raise only after leaving handlers: raw exceptions must not become context.
    if failures:
        pytest.fail("Dev CAPP hello failed: " + "; ".join(failures), pytrace=False)
