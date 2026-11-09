"""Async orchestration cancellation and thread boundaries without a database."""

import asyncio
import threading
from types import SimpleNamespace
from uuid import UUID

import pytest

from src import config
from src.bl import builds
from src.models.api.ci_webhook import CiWebhookIn
from tests.capp_fakes import NAME


@pytest.fixture(autouse=True)
def clean_tables():
    yield


@pytest.fixture
def build_event(monkeypatch):
    automation_id = UUID(NAME.removeprefix("automation-"))
    repository = "registry.example/automation"
    monkeypatch.setattr(config, "AUTOMATION_IMAGE_REPOSITORY", repository)
    return CiWebhookIn(
        automation_id=automation_id,
        commit_sha="a" * 40,
        status="success",
        image=f"{repository}/{automation_id}@sha256:" + "a" * 64,
        contract_version="1",
    )


async def test_cancellation_during_deploy_never_commits(build_event, monkeypatch):
    owner = threading.get_ident()
    entered = asyncio.Event()
    commits = []

    def snapshot(event):
        assert threading.get_ident() != owner
        return builds._BuildTarget("tenant", "team", None, None)

    def commit(*args):
        commits.append(args)

    class Deployment:
        async def create_deployment(self, **kwargs):
            assert threading.get_ident() == owner
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(builds, "_read_target", snapshot)
    monkeypatch.setattr(builds, "_commit_success", commit)
    task = asyncio.create_task(
        builds.apply_build_result(build_event, Deployment(), None, None)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert commits == []


async def test_database_and_publishers_offloaded_while_capp_is_awaited(
    build_event, monkeypatch
):
    owner = threading.get_ident()
    calls = []

    def snapshot(event):
        assert threading.get_ident() != owner
        calls.append("snapshot")
        return builds._BuildTarget("tenant", "team", None, None)

    def commit(*args):
        assert threading.get_ident() != owner
        calls.append("commit")
        return None, {"type": "build"}

    class Deployment:
        async def create_deployment(self, **kwargs):
            assert threading.get_ident() == owner
            await asyncio.sleep(0)
            calls.append("deploy")
            return SimpleNamespace(
                deployment_id="uid", deployment_url="https://run.example"
            )

    class Reload:
        def publish_reload(self):
            assert threading.get_ident() != owner
            calls.append("reload")

    class Events:
        def publish(self, event):
            assert threading.get_ident() != owner
            calls.append("publish")

    monkeypatch.setattr(builds, "_read_target", snapshot)
    monkeypatch.setattr(builds, "_commit_success", commit)
    result = await builds.apply_build_result(
        build_event, Deployment(), Reload(), Events()
    )
    assert result.status == "applied"
    assert calls == ["snapshot", "deploy", "commit", "reload", "publish"]
