"""Opt-in dev CAPP acceptance; never runs from ordinary CI configuration.

The supplied hello image must read /etc/automation/run-auth/api-key and expose
the wrapper's X-API-Key protected POST /run. This is F25-style orchestration,
deliberately outside the management client and the D17 submit retry loop.
"""

import json
from http import HTTPStatus
import os
import secrets
import time
from urllib.request import Request, build_opener
from uuid import uuid4

import pytest

from src.bl.capp_client import CappClient, _NoRedirect
from src.bl.capp_models import CappRequest, SecretRequest
from src.config import load_capp_client_settings


@pytest.fixture(autouse=True)
def clean_tables():
    """This external acceptance test must not acquire a pooled DB connection."""
    yield


@pytest.mark.skipif(
    os.environ.get("CAPP_LIVE_TEST") != "1", reason="Dev CAPP acceptance is opt-in"
)
def test_dev_hello_round_trip():
    adapter = CappClient(**load_capp_client_settings())
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
    failure = None
    cleanup_failed = False
    try:
        adapter.create_secret(wallet, SecretRequest(secret_name, {"api-key": key}))
        adapter.create_capp(wallet, request)
        deadline = time.monotonic() + 600
        while True:
            resource = adapter.get_capp(wallet, name)
            ready = [c for c in resource.conditions if c.type == "Ready"]
            if (
                resource.image == image
                and ready
                and all(c.status == "True" for c in ready)
            ):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("readiness deadline")
            time.sleep(5)
        if resource.deployment_url != "https://" + hostname:
            raise RuntimeError("unexpected route")
        # Service identity stays in CappClient. Invocation uses only this
        # disposable deployment's run key; no retries and no raw output.
        invocation = Request(
            resource.deployment_url + "/run",
            method="POST",
            data=b'{"message":"D16 hello verification"}',
            headers={
                "Content-Type": "application/json",
                "X-API-Key": key,
                "X-Run-Id": str(uuid4()),
                "X-History-Id": "d16-hello",
                "X-Attempt": "1",
            },
        )
        with build_opener(_NoRedirect()).open(invocation, timeout=30) as response:
            if (
                response.status != HTTPStatus.OK
                or json.loads(response.read(65536)).get("status") != "ok"
            ):
                raise RuntimeError("hello failed")
    except Exception:
        # Neither transport exceptions nor response bodies may reach pytest.
        failure = "Dev CAPP hello round-trip failed"
    finally:
        try:
            adapter.delete_capp(wallet, name)
            adapter.delete_secret(wallet, secret_name)
        except Exception:
            cleanup_failed = True
    if cleanup_failed:
        pytest.fail(f"Dev CAPP cleanup failed; inspect resource {name}", pytrace=False)
    if failure:
        pytest.fail(failure, pytrace=False)
