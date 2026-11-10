"""HTTP webhook -> real async CAPP coordinator -> database cutover."""

import asyncio
import copy
import json
import threading
from uuid import uuid4

import httpx
import pytest
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError

from src import config
from src.api import deps
from src.bl import builds
from src.bl.capp.client import CappClient
from tests.capp_fakes import NOW, IDENTITY, capp, tokens
from tests.test_ci_webhook import (
    harness,
    webhook_config,
    image,
    row,
    success,
    failed,
)


class CappBackend:
    def __init__(self, engine):
        self.engine = engine
        self.resources = {}
        self.calls = []
        self.pending = False
        self.fail_put = False
        self.on_get = None
        self.loop = None
        self.thread = None

    async def __call__(self, request):
        loop = asyncio.get_running_loop()
        if self.loop is None:
            self.loop = loop
            self.thread = threading.get_ident()
        assert loop is self.loop
        assert self.engine.pool.checkedout() == 0
        assert request.extensions["timeout"] == dict(connect=7, read=7, write=7, pool=7)
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))
        if path.endswith("/auth/login"):
            return httpx.Response(200, json=tokens())
        if "/secrets/" in path:
            assert method == "DELETE"
            return httpx.Response(404)
        wallet = path.split("/namespaces/")[1].split("/")[0]
        name = body["name"] if method == "POST" else path.rsplit("/", 1)[-1]
        key = (wallet, name)
        if method == "POST":
            if key in self.resources:
                return httpx.Response(409)
            current = capp(
                name=name,
                namespace=wallet,
                uid=str(uuid4()),
                **{k: v for k, v in body.items() if k != "name"},
            )
            current["routeSpec"] = {
                "hostname": "automation.example",
                "tlsEnabled": True,
            }
            self.resources[key] = current
            return httpx.Response(201, json=current)
        if method == "DELETE":
            self.resources.pop(key, None)
            return httpx.Response(204)
        if key not in self.resources:
            return httpx.Response(404)
        if method == "PUT":
            if self.fail_put:
                status = 403 if self.fail_put == "permanent" else 503
                return httpx.Response(status, json={"message": IDENTITY[1]})
            self.resources[key].update(body)
            return httpx.Response(200, json=self.resources[key])
        assert method == "GET"
        if self.on_get:
            hook, self.on_get = self.on_get, None
            await run_in_threadpool(hook)
        current = copy.deepcopy(self.resources[key])
        if self.pending:
            current["status"]["conditions"][0]["status"] = "Unknown"
        return httpx.Response(200, json=current)


@pytest.fixture
def real_backend(test_engine, monkeypatch):
    backend = CappBackend(test_engine)

    def build():
        return CappClient(
            base_url="https://capp.example",
            username=IDENTITY[0],
            password=IDENTITY[1],
            cluster="dev",
            timeout_seconds=7,
            clock=lambda: NOW,
            transport=httpx.MockTransport(backend),
        )

    monkeypatch.setattr(deps, "build_capp_client", build)
    monkeypatch.setattr(config, "CAPP_READINESS_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(config, "CAPP_POLL_INTERVAL_SECONDS", 0.001)
    return backend


@pytest.fixture
def wire_harness(real_backend, harness):
    # Exercise the production startup dependency, not a coordinator override.
    harness.client.app.dependency_overrides.pop(deps.get_capp_client)
    return harness, real_backend


def test_real_create_records_uid_url_then_rollout_removes_secret(
    wire_harness, test_engine
):
    harness, backend = wire_harness
    created = harness.create()
    automation_id = created["id"]
    before = row(test_engine, automation_id)
    sql_threads = []

    def sql(*args):
        sql_threads.append(threading.get_ident())

    event.listen(test_engine, "before_cursor_execute", sql)
    try:
        response = harness.webhook(success(automation_id, before["building_sha"]))
        assert response.status_code == 200, response.text
        assert all(thread != backend.thread for thread in sql_threads)
    finally:
        event.remove(test_engine, "before_cursor_execute", sql)
    deployed = row(test_engine, automation_id)
    resource = backend.resources[("payments-wallet", "automation-" + automation_id)]
    assert deployed["capp_deployment_id"] == resource["name"]
    assert deployed["deployment_url"] == "https://automation.example"
    assert deployed["active_digest"] == image(automation_id)
    assert resource["secretVolumes"][0]["secretName"] == "payments-secret"
    harness.edit(automation_id, secret_name=None)
    sha = row(test_engine, automation_id)["building_sha"]
    response = harness.webhook(success(automation_id, sha, version=2))
    assert response.status_code == 200, response.text
    updated = row(test_engine, automation_id)
    assert updated["active_digest"] == image(automation_id, 2)
    assert updated["capp_deployment_id"] == deployed["capp_deployment_id"]
    assert resource["secretVolumes"] == []
    assert any(
        method == "PUT" and path.endswith("automation-" + automation_id)
        for method, path, _ in backend.calls
    )


@pytest.mark.parametrize("failure", ["upstream", "readiness"])
def test_failed_rollout_retains_digest_and_build_lock(
    wire_harness, test_engine, failure, caplog
):
    harness, backend = wire_harness
    created = harness.create()
    automation_id = created["id"]
    sha = row(test_engine, automation_id)["building_sha"]
    assert harness.webhook(success(automation_id, sha)).status_code == 200
    harness.edit(automation_id)
    before = row(test_engine, automation_id)
    backend.fail_put = failure == "upstream"
    backend.pending = failure == "readiness"
    response = harness.webhook(
        success(automation_id, before["building_sha"], version=2)
    )
    # Upstream and readiness failures are transient and retain the build lock.
    assert response.status_code == 503
    after = row(test_engine, automation_id)
    for key in ("active_digest", "capp_deployment_id", "deployment_url"):
        assert after[key] == before[key]
    assert after["build_state"] == "building"
    assert after["building_sha"] == before["building_sha"]
    assert after["build_lock_deadline"] == before["build_lock_deadline"]
    assert all(secret not in response.text + caplog.text for secret in IDENTITY)


def test_retry_after_database_failure_recovers_created_resource(
    wire_harness, test_engine, monkeypatch
):
    harness, backend = wire_harness
    created = harness.create()
    automation_id = created["id"]
    payload = success(automation_id, row(test_engine, automation_id)["building_sha"])
    original = builds._cut_over

    def fail_commit(*args):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(builds, "_cut_over", fail_commit)
    assert harness.webhook(payload).status_code == 503
    assert row(test_engine, automation_id)["capp_deployment_id"] is None
    deployment_name = next(iter(backend.resources.values()))["name"]
    monkeypatch.setattr(builds, "_cut_over", original)
    assert harness.webhook(payload).status_code == 200
    assert row(test_engine, automation_id)["capp_deployment_id"] == deployment_name
    assert len(backend.resources) == 1
    assert not any(method == "PUT" for method, _, _ in backend.calls)


@pytest.mark.parametrize("pending", [False, True])
def test_deletion_race_cleans_up_by_name_after_uid_create(
    wire_harness, test_engine, pending
):
    harness, backend = wire_harness
    created = harness.create()
    automation_id = created["id"]
    sha = row(test_engine, automation_id)["building_sha"]

    def delete_during_readiness():
        with test_engine.begin() as conn:
            conn.execute(
                text("UPDATE automations SET matching_state='deleting' WHERE id=:id"),
                {"id": automation_id},
            )

    backend.on_get = delete_during_readiness
    backend.pending = pending
    response = harness.webhook(success(automation_id, sha))
    if pending:
        assert response.status_code == 503
    else:
        assert response.json() == {"status": "ignored", "reason": "deleting"}
    deletes = [path for method, path, _ in backend.calls if method == "DELETE"]
    assert deletes == [
        f"/api/v1/clusters/dev/namespaces/payments-wallet/capps/automation-{automation_id}",
        f"/api/v1/clusters/dev/namespaces/payments-wallet/secrets/automation-{automation_id}-run-auth",
    ]
    assert not backend.resources
    assert row(test_engine, automation_id)["active_digest"] is None


def test_build_failure_does_not_contact_capp(wire_harness, test_engine):
    harness, backend = wire_harness
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    assert harness.webhook(failed(created["id"], sha)).status_code == 200
    assert backend.calls == []
