"""CI webhook (D15, spec §8.2, §5.1): token gate, cutover, failure, idempotency."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from src import config
from src.api import deps
from src.api.deps import (
    get_capp_client,
    get_event_publisher,
    get_git_client,
    get_reload_publisher,
)
from src.bl.capp_client import InMemoryCappClient
from src.bl.git_client import InMemoryGitClient
from src.main import get_app
from tests.conftest import TENANT

TOKEN = "ci-secret-token"
AUTH = {"X-Gitlab-Token": TOKEN}
IMAGE_V1 = "artifactory.test/automations/a@sha256:" + "1" * 64
IMAGE_V2 = "artifactory.test/automations/a@sha256:" + "2" * 64

AUTOMATION = {
    "name": "restart-payments",
    "namespace": "payments-wallet",
    "script": "def handle(alert):\n    return {}\n",
    "secret_name": "payments-secret",
    "triggers": [
        {"field": "severity", "value": "critical"},
        {"field": "application", "value": "payments"},
    ],
}


class RecordingReload:
    def __init__(self):
        self.count = 0

    def publish_reload(self) -> None:
        self.count += 1


class RecordingEvents:
    def __init__(self):
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


class Harness:
    def __init__(self, client: TestClient, capp, reload, events):
        self.client = client
        self.capp = capp
        self.reload = reload
        self.events = events

    def create(self) -> dict:
        resp = self.client.post("/automations", json=AUTOMATION)
        assert resp.status_code == 201
        return resp.json()

    def edit(self, automation_id: str) -> None:
        resp = self.client.put(
            f"/automations/{automation_id}",
            json={**AUTOMATION, "script": "def handle(alert):\n    return {'v': 2}\n"},
        )
        assert resp.status_code == 200, resp.text

    def webhook(self, payload: dict, headers: dict | None = AUTH):
        return self.client.post("/internal/ci-webhook", json=payload, headers=headers)


@pytest.fixture()
def harness(test_engine, monkeypatch):
    monkeypatch.setattr(config, "CI_WEBHOOK_TOKEN", TOKEN)
    app = get_app()
    git = InMemoryGitClient()
    capp = InMemoryCappClient()
    reload = RecordingReload()
    events = RecordingEvents()
    app.dependency_overrides[get_git_client] = lambda: git
    app.dependency_overrides[get_capp_client] = lambda: capp
    app.dependency_overrides[get_reload_publisher] = lambda: reload
    app.dependency_overrides[get_event_publisher] = lambda: events
    with TestClient(app) as client:
        yield Harness(client, capp, reload, events)


def row(test_engine, automation_id: str) -> dict:
    with test_engine.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT * FROM automations WHERE id = :id"), {"id": automation_id}
            ).mappings().one()
        )


def revisions(test_engine, automation_id: str) -> list[dict]:
    with test_engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT action, git_sha, resulting_digest FROM automation_revisions "
                    "WHERE automation_id = :id ORDER BY created_at"
                ),
                {"id": automation_id},
            ).mappings()
        ]


def success(automation_id: str, sha: str, image: str = IMAGE_V1) -> dict:
    return {
        "automation_id": automation_id,
        "commit_sha": sha,
        "status": "success",
        "image": image,
        "contract_version": "1",
    }


def failed(automation_id: str, sha: str) -> dict:
    return {"automation_id": automation_id, "commit_sha": sha, "status": "failed"}


# --- build lock armed by authoring -------------------------------------------

def test_create_arms_build_lock_with_commit_sha(harness, test_engine):
    created = harness.create()
    automation = row(test_engine, created["id"])
    assert automation["build_state"] == "building"
    assert automation["building_sha"] == revisions(test_engine, created["id"])[0]["git_sha"]
    remaining = automation["build_lock_deadline"] - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < remaining <= timedelta(minutes=30)


# --- token gate ----------------------------------------------------------------

@pytest.mark.parametrize(
    "headers",
    [None, {"X-Gitlab-Token": "wrong"}, {"X-Gitlab-Token": ""}],
    ids=["missing", "wrong", "empty"],
)
def test_bad_token_is_401_and_changes_nothing(harness, test_engine, headers):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    resp = harness.webhook(success(created["id"], sha), headers=headers)
    assert resp.status_code == 401
    assert row(test_engine, created["id"])["build_state"] == "building"
    assert harness.capp.calls == []


def test_unconfigured_token_fails_closed(harness, test_engine, monkeypatch):
    monkeypatch.setattr(config, "CI_WEBHOOK_TOKEN", "")
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    # Even a caller sending an empty token must not match an empty secret.
    for headers in (None, {"X-Gitlab-Token": ""}):
        assert harness.webhook(success(created["id"], sha), headers=headers).status_code == 401


def test_auth_is_checked_before_the_body(harness):
    # An unauthenticated caller learns nothing about the payload schema.
    resp = harness.webhook({"garbage": True}, headers=None)
    assert resp.status_code == 401
    assert "errors" not in resp.json()


# --- payload validation --------------------------------------------------------

def test_success_without_image_or_contract_version_is_400(harness):
    payload = {"automation_id": str(uuid4()), "commit_sha": "a" * 40, "status": "success"}
    resp = harness.webhook(payload)
    assert resp.status_code == 400
    errors = {(e["field"], e["code"]) for e in resp.json()["errors"]}
    assert ("image", "field_required") in errors
    assert ("contract_version", "field_required") in errors


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"image": "artifactory.test/automations/a:latest"}, "image"),
        ({"commit_sha": "not-a-sha"}, "commit_sha"),
        ({"status": "cancelled"}, "status"),
        ({"automation_id": "nope"}, "automation_id"),
    ],
)
def test_malformed_fields_are_400_invalid_value(harness, overrides, field):
    payload = {**success(str(uuid4()), "a" * 40), **overrides}
    resp = harness.webhook(payload)
    assert resp.status_code == 400
    assert any(e["field"] == field and e["code"] == "invalid_value" for e in resp.json()["errors"])


def test_failure_payload_needs_no_image(harness):
    created = harness.create()
    resp = harness.webhook(failed(created["id"], "a" * 40))
    assert resp.status_code == 200


def test_unknown_automation_is_404(harness):
    assert harness.webhook(success(str(uuid4()), "a" * 40)).status_code == 404


# --- first build ------------------------------------------------------------

def test_first_build_success_deploys_and_cuts_over(harness, test_engine):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]

    resp = harness.webhook(success(created["id"], sha))

    assert resp.status_code == 200
    assert resp.json() == {"status": "applied"}
    assert harness.capp.calls == [
        ("create", created["id"], "payments-wallet", IMAGE_V1, ("payments-secret",))
    ]
    automation = row(test_engine, created["id"])
    assert automation["active_digest"] == IMAGE_V1
    assert automation["contract_version"] == "1"
    assert automation["capp_deployment_id"] == f"capp-{created['id']}"
    assert automation["deployment_url"].endswith(created["id"])
    assert automation["build_state"] == "idle"
    assert automation["building_sha"] is None
    assert automation["build_lock_deadline"] is None
    # First build lands inactive — "ready to enable", never auto-armed.
    assert automation["matching_state"] == "inactive"
    assert revisions(test_engine, created["id"]) == [
        {"action": "create", "git_sha": sha, "resulting_digest": IMAGE_V1}
    ]
    assert harness.reload.count == 1
    assert harness.events.events == [
        {
            "type": "build",
            "automation_id": created["id"],
            "tenant_id": TENANT,
            "commit_sha": sha,
            "build_state": "idle",
            "matching_state": "inactive",
            "active_digest": IMAGE_V1,
        }
    ]


def test_first_build_failure_marks_build_failed(harness, test_engine):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]

    resp = harness.webhook(failed(created["id"], sha))

    assert resp.json() == {"status": "applied"}
    automation = row(test_engine, created["id"])
    assert automation["build_state"] == "build_failed"
    assert automation["active_digest"] is None
    assert automation["capp_deployment_id"] is None
    assert automation["building_sha"] is None
    assert automation["build_lock_deadline"] is None
    assert harness.capp.calls == []
    assert harness.reload.count == 0
    assert harness.events.events[-1]["build_state"] == "build_failed"
    # The author can retry with an edit: the mid-build 409 no longer applies.
    harness.edit(created["id"])


# --- edits --------------------------------------------------------------------

def _built(harness, test_engine) -> str:
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    assert harness.webhook(success(created["id"], sha)).status_code == 200
    return created["id"]


def test_edit_build_success_rolls_out_existing_deployment(harness, test_engine):
    automation_id = _built(harness, test_engine)
    harness.edit(automation_id)
    sha = row(test_engine, automation_id)["building_sha"]

    resp = harness.webhook(success(automation_id, sha, IMAGE_V2))

    assert resp.json() == {"status": "applied"}
    assert harness.capp.calls[-1] == ("rollout", f"capp-{automation_id}", IMAGE_V2)
    assert len([c for c in harness.capp.calls if c[0] == "create"]) == 1
    automation = row(test_engine, automation_id)
    assert automation["active_digest"] == IMAGE_V2
    assert automation["capp_deployment_id"] == f"capp-{automation_id}"
    assert automation["build_state"] == "idle"
    assert revisions(test_engine, automation_id)[-1] == {
        "action": "edit",
        "git_sha": sha,
        "resulting_digest": IMAGE_V2,
    }


def test_edit_of_active_automation_keeps_it_active(harness, test_engine):
    automation_id = _built(harness, test_engine)
    with test_engine.begin() as conn:
        conn.execute(
            text("UPDATE automations SET matching_state = 'active' WHERE id = :id"),
            {"id": automation_id},
        )
    harness.edit(automation_id)
    sha = row(test_engine, automation_id)["building_sha"]
    harness.webhook(success(automation_id, sha, IMAGE_V2))
    automation = row(test_engine, automation_id)
    assert automation["matching_state"] == "active"
    assert automation["active_digest"] == IMAGE_V2


def test_failed_edit_build_keeps_old_digest_serving(harness, test_engine):
    automation_id = _built(harness, test_engine)
    calls_before = list(harness.capp.calls)
    harness.edit(automation_id)
    sha = row(test_engine, automation_id)["building_sha"]

    resp = harness.webhook(failed(automation_id, sha))

    assert resp.json() == {"status": "applied"}
    automation = row(test_engine, automation_id)
    assert automation["build_state"] == "build_failed"
    assert automation["active_digest"] == IMAGE_V1
    assert harness.capp.calls == calls_before
    assert harness.capp.images[f"capp-{automation_id}"] == IMAGE_V1


# --- CAPP failure -------------------------------------------------------------

def test_capp_failure_is_503_and_leaves_build_locked(harness, test_engine):
    created = harness.create()
    before = row(test_engine, created["id"])
    harness.capp.fail = True

    resp = harness.webhook(success(created["id"], before["building_sha"]))

    assert resp.status_code == 503
    assert "CAPP" not in resp.text
    after = row(test_engine, created["id"])
    for column in ("build_state", "building_sha", "build_lock_deadline", "active_digest",
                   "capp_deployment_id"):
        assert after[column] == before[column]
    assert harness.reload.count == 0
    assert harness.events.events == []

    # Redelivery once CAPP recovers completes the cutover.
    harness.capp.fail = False
    resp = harness.webhook(success(created["id"], before["building_sha"]))
    assert resp.json() == {"status": "applied"}
    assert row(test_engine, created["id"])["active_digest"] == IMAGE_V1


def test_failed_rollout_keeps_old_digest(harness, test_engine):
    automation_id = _built(harness, test_engine)
    harness.edit(automation_id)
    sha = row(test_engine, automation_id)["building_sha"]
    harness.capp.fail = True

    assert harness.webhook(success(automation_id, sha, IMAGE_V2)).status_code == 503
    automation = row(test_engine, automation_id)
    assert automation["active_digest"] == IMAGE_V1
    assert automation["build_state"] == "building"


def test_production_default_capp_client_fails_closed(test_engine, monkeypatch):
    # Until D16 the real client is absent: the webhook must 503, never cut over
    # to an image no deployment serves.
    monkeypatch.setattr(config, "CI_WEBHOOK_TOKEN", TOKEN)
    app = get_app()
    git = InMemoryGitClient()
    app.dependency_overrides[get_git_client] = lambda: git
    app.dependency_overrides[get_reload_publisher] = lambda: RecordingReload()
    app.dependency_overrides[get_event_publisher] = lambda: RecordingEvents()
    assert deps.get_capp_client not in app.dependency_overrides
    with TestClient(app) as client:
        created = client.post("/automations", json=AUTOMATION).json()
        sha = row(test_engine, created["id"])["building_sha"]
        resp = client.post(
            "/internal/ci-webhook", json=success(created["id"], sha), headers=AUTH
        )
    assert resp.status_code == 503
    assert row(test_engine, created["id"])["build_state"] == "building"


# --- idempotency ----------------------------------------------------------------

def test_replayed_success_is_a_noop(harness, test_engine):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    harness.webhook(success(created["id"], sha))
    state = row(test_engine, created["id"])
    calls, reloads, events = list(harness.capp.calls), harness.reload.count, len(harness.events.events)

    resp = harness.webhook(success(created["id"], sha))

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "not_building"}
    assert row(test_engine, created["id"]) == state
    assert harness.capp.calls == calls
    assert harness.reload.count == reloads
    assert len(harness.events.events) == events


def test_replayed_failure_after_success_cannot_undo_cutover(harness, test_engine):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    harness.webhook(success(created["id"], sha))
    resp = harness.webhook(failed(created["id"], sha))
    assert resp.json()["status"] == "ignored"
    assert row(test_engine, created["id"])["build_state"] == "idle"


def test_superseded_sha_is_ignored(harness, test_engine):
    automation_id = _built(harness, test_engine)
    first_sha = revisions(test_engine, automation_id)[0]["git_sha"]
    harness.edit(automation_id)
    calls = list(harness.capp.calls)

    # A late webhook for the earlier commit must not cut the edit's build over.
    resp = harness.webhook(success(automation_id, first_sha, IMAGE_V2))

    assert resp.json() == {"status": "ignored", "reason": "sha_mismatch"}
    automation = row(test_engine, automation_id)
    assert automation["build_state"] == "building"
    assert automation["active_digest"] == IMAGE_V1
    assert harness.capp.calls == calls


@pytest.mark.parametrize("matching_state", ["deleting", "deleted"])
def test_deleting_automation_is_never_deployed(harness, test_engine, matching_state):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    with test_engine.begin() as conn:
        conn.execute(
            text("UPDATE automations SET matching_state = :s WHERE id = :id"),
            {"s": matching_state, "id": created["id"]},
        )
    resp = harness.webhook(success(created["id"], sha))
    assert resp.json() == {"status": "ignored", "reason": "deleting"}
    assert harness.capp.calls == []
    assert row(test_engine, created["id"])["active_digest"] is None


# --- connection discipline + races (BL level) ----------------------------------

class _ProbingCapp(InMemoryCappClient):
    """Records how many pooled connections are checked out during each call."""

    def __init__(self, engine, on_create=None):
        super().__init__()
        self.engine = engine
        self.on_create = on_create
        self.checked_out: list[int] = []

    def create_deployment(self, **kwargs):
        self.checked_out.append(self.engine.pool.checkedout())
        if self.on_create is not None:
            self.on_create()
        return super().create_deployment(**kwargs)

    def rollout(self, **kwargs):
        self.checked_out.append(self.engine.pool.checkedout())
        return super().rollout(**kwargs)


def _event(automation_id: str, sha: str):
    from src.models.api.ci_webhook import CiWebhookIn

    return CiWebhookIn(**success(automation_id, sha))


def test_no_db_connection_is_held_across_the_capp_call(harness, test_engine):
    from src.bl import builds

    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    capp = _ProbingCapp(test_engine)
    builds.apply_build_result(_event(created["id"], sha), capp, RecordingReload(), RecordingEvents())
    assert capp.checked_out == [0]


def test_concurrent_duplicate_delivery_cuts_over_once(harness, test_engine):
    from src.bl import builds

    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    rival_events = RecordingEvents()

    # The rival delivery completes while this one is inside its CAPP call.
    def rival():
        builds.apply_build_result(
            _event(created["id"], sha), InMemoryCappClient(), RecordingReload(), rival_events
        )

    events = RecordingEvents()
    reload = RecordingReload()
    result = builds.apply_build_result(
        _event(created["id"], sha), _ProbingCapp(test_engine, on_create=rival), reload, events
    )

    assert result.status == "ignored"
    assert len(rival_events.events) == 1
    assert events.events == [] and reload.count == 0
    assert row(test_engine, created["id"])["active_digest"] == IMAGE_V1
