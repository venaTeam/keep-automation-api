"""CI webhook (D15, spec §8.2, §5.1): token gate, cutover, failure, idempotency."""
import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src import config
from src.api import deps
from src.api.ci_webhook_guard import CI_WEBHOOK_PATH, CiWebhookGuard
from src.api.deps import (
    get_authenticated_entity,
    get_capp_client,
    get_event_publisher,
    get_git_client,
    get_reload_publisher,
)
from src.bl import builds
from src.bl.capp_client import InMemoryCappClient
from src.bl.git_client import InMemoryGitClient
from src.main import get_app
from src.models.api.ci_webhook import CiWebhookIn
from src.models.api.identity import AuthenticatedEntity
from tests.conftest import OTHER_TENANT, TENANT

TOKEN = "ci-secret-token"
AUTH = {"X-Gitlab-Token": TOKEN}
REPOSITORY = "artifactory.test/automations"
FIXTURES = Path(__file__).parent / "fixtures"

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


def image(automation_id: str, version: int = 1) -> str:
    """A digest-pinned image in the automation's own repository."""
    return f"{REPOSITORY}/{automation_id}@sha256:" + str(version) * 64


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


class RecordingDeletion:
    """The cascade's CAPP deletion interface; absent = 404 = success."""

    def __init__(self):
        self.calls: list[tuple] = []

    def delete_capp(self, wallet: str, name: str) -> None:
        self.calls.append(("delete_capp", wallet, name))

    def delete_secret(self, wallet: str, name: str) -> None:
        self.calls.append(("delete_secret", wallet, name))


class Harness:
    def __init__(self, client: TestClient, capp, reload, events):
        self.client = client
        self.capp = capp
        self.reload = reload
        self.events = events

    def create(self, **overrides) -> dict:
        resp = self.client.post("/automations", json={**AUTOMATION, **overrides})
        assert resp.status_code == 201
        return resp.json()

    def act_as(self, tenant_id: str) -> None:
        self.client.app.dependency_overrides[get_authenticated_entity] = (
            lambda: AuthenticatedEntity(tenant_id=tenant_id, email=f"author@{tenant_id}")
        )

    def edit(self, automation_id: str, expect: int = 200, **overrides):
        resp = self.client.put(
            f"/automations/{automation_id}",
            json={
                **AUTOMATION,
                "script": "def handle(alert):\n    return {'v': 2}\n",
                **overrides,
            },
        )
        assert resp.status_code == expect, resp.text
        return resp

    def webhook(self, payload: dict, headers: dict | None = AUTH):
        return self.client.post("/internal/ci-webhook", json=payload, headers=headers)

    def counts(self) -> tuple:
        return list(self.capp.calls), self.reload.count, len(self.events.events)


@pytest.fixture(autouse=True)
def webhook_config(monkeypatch):
    monkeypatch.setattr(config, "CI_WEBHOOK_TOKEN", TOKEN)
    monkeypatch.setattr(config, "AUTOMATION_IMAGE_REPOSITORY", REPOSITORY)
    # The rejection-log throttles are process-wide; give every test fresh ones
    # so a log assertion never depends on which test rejected a token first.
    monkeypatch.setattr(deps, "_unconfigured_log", deps._LogThrottle(60))
    monkeypatch.setattr(deps, "_mismatch_log", deps._LogThrottle(60))


@pytest.fixture()
def harness(test_engine):
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
                    "SELECT action, git_sha, resulting_digest, actor FROM automation_revisions "
                    "WHERE automation_id = :id ORDER BY created_at"
                ),
                {"id": automation_id},
            ).mappings()
        ]


def success(automation_id: str, sha: str, version: int = 1, **overrides) -> dict:
    return {
        "automation_id": automation_id,
        "commit_sha": sha,
        "status": "success",
        "image": image(automation_id, version),
        "contract_version": "1",
        **overrides,
    }


def failed(automation_id: str, sha: str) -> dict:
    return {"automation_id": automation_id, "commit_sha": sha, "status": "failed"}


def assert_lock_armed(automation: dict) -> None:
    assert automation["build_state"] == "building"
    remaining = automation["build_lock_deadline"] - datetime.now(timezone.utc)
    full = timedelta(seconds=config.BUILD_LOCK_TIMEOUT_SECONDS)
    assert full - timedelta(minutes=1) < remaining <= full


# --- build lock armed by authoring -------------------------------------------

def test_create_arms_build_lock_with_commit_sha(harness, test_engine):
    created = harness.create()
    automation = row(test_engine, created["id"])
    assert_lock_armed(automation)
    assert automation["building_sha"] == revisions(test_engine, created["id"])[0]["git_sha"]


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


def test_wrong_token_is_logged_at_most_once_per_window(harness, caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            harness.webhook(success(str(uuid4()), "a" * 40), headers={"X-Gitlab-Token": "x"})
    mismatches = [r for r in caplog.records if "does not match" in r.getMessage()]
    assert len(mismatches) == 1
    assert TOKEN not in caplog.text


def test_unconfigured_token_is_logged_at_most_once_per_window(harness, caplog, monkeypatch):
    monkeypatch.setattr(config, "CI_WEBHOOK_TOKEN", "")
    with caplog.at_level(logging.ERROR):
        for _ in range(5):
            harness.webhook(success(str(uuid4()), "a" * 40))
    assert len([r for r in caplog.records if "not configured" in r.getMessage()]) == 1


def test_log_throttle_reports_what_it_suppressed(monkeypatch):
    clock = iter([0.0, 1.0, 2.0, 61.0])
    monkeypatch.setattr(deps.time, "monotonic", lambda: next(clock))
    throttle = deps._LogThrottle(60)
    assert [throttle.allow() for _ in range(4)] == [0, None, None, 2]


def test_unconfigured_token_fails_closed(harness, test_engine, monkeypatch):
    monkeypatch.setattr(config, "CI_WEBHOOK_TOKEN", "")
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    # Even a caller sending an empty token must not match an empty secret.
    for headers in (None, {"X-Gitlab-Token": ""}):
        assert harness.webhook(success(created["id"], sha), headers=headers).status_code == 401


def test_auth_is_checked_before_the_body_is_read(harness):
    # Malformed JSON without a token: a 401, not a JSON-decode 400 — the body
    # is never read for an unauthenticated caller.
    resp = harness.client.post(
        "/internal/ci-webhook",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert "errors" not in resp.json()


def test_oversized_body_is_413_before_read(harness, monkeypatch):
    monkeypatch.setattr(config, "CI_WEBHOOK_MAX_BODY_BYTES", 64)
    resp = harness.client.post(
        "/internal/ci-webhook",
        content=b"x" * 65,
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert resp.status_code == 413


def test_missing_content_length_is_411(harness):
    def chunks():
        yield b"{}"

    resp = harness.client.post(
        "/internal/ci-webhook",
        content=chunks(),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert resp.status_code == 411


def guard_status(headers: dict[str, str]) -> int:
    """Run the guard alone on raw ASGI headers (an HTTP client would normalise
    framing headers); a request that passes through reaches a 204 app."""
    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        raise AssertionError("the guard must answer without reading the body")

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": CI_WEBHOOK_PATH,
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    asyncio.run(CiWebhookGuard(downstream)(scope, receive, send))
    return sent[0]["status"]


@pytest.mark.parametrize(
    "length, expected",
    [("64", 204), ("65", 413), ("0", 204), ("-1", 411), ("abc", 411), (None, 411)],
    ids=["at-limit", "over-limit", "empty", "negative", "garbage", "missing"],
)
def test_guard_content_length_edges(monkeypatch, length, expected):
    monkeypatch.setattr(config, "CI_WEBHOOK_MAX_BODY_BYTES", 64)
    headers = {**AUTH} if length is None else {**AUTH, "Content-Length": length}
    assert guard_status(headers) == expected


@pytest.mark.parametrize("encoding", ["chunked", "gzip, chunked", "identity"])
def test_guard_refuses_transfer_encoding_even_with_a_small_length(encoding):
    # h11 lets chunked framing win over Content-Length, so a small declared
    # length must not admit an unbounded chunked body.
    assert guard_status({**AUTH, "Content-Length": "10", "Transfer-Encoding": encoding}) == 411


def test_guard_checks_the_token_before_framing():
    assert guard_status({"Transfer-Encoding": "chunked"}) == 401


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
        ({"image": REPOSITORY + "/a:latest"}, "image"),
        ({"image": REPOSITORY + "/a@sha256:" + "1" * 64 + "\n"}, "image"),
        ({"image": "Artifactory.test/a@sha256:" + "1" * 64}, "image"),
        ({"commit_sha": "not-a-sha"}, "commit_sha"),
        ({"commit_sha": "A" * 40}, "commit_sha"),
        ({"commit_sha": "a" * 40 + "\n"}, "commit_sha"),
        ({"contract_version": " "}, "contract_version"),
        ({"contract_version": "v" * 65}, "contract_version"),
        ({"status": "cancelled"}, "status"),
        ({"automation_id": "nope"}, "automation_id"),
    ],
)
def test_malformed_fields_are_400_invalid_value(harness, overrides, field):
    payload = {**success(str(uuid4()), "a" * 40), **overrides}
    resp = harness.webhook(payload)
    assert resp.status_code == 400
    assert any(e["field"] == field and e["code"] == "invalid_value" for e in resp.json()["errors"])


def test_sha256_repository_commit_ids_are_accepted():
    CiWebhookIn(**success(str(uuid4()), "b" * 64))


def test_failure_payload_ignores_image():
    event = CiWebhookIn(**{**failed(str(uuid4()), "a" * 40), "image": "garbage"})
    assert event.image == "garbage"


def test_unknown_automation_is_404(harness):
    assert harness.webhook(success(str(uuid4()), "a" * 40)).status_code == 404


# --- image binding --------------------------------------------------------------

@pytest.mark.parametrize(
    "foreign_image",
    [
        "evil.example/x@sha256:" + "1" * 64,
        f"{REPOSITORY}/{uuid4()}@sha256:" + "1" * 64,  # another automation's
    ],
    ids=["foreign-registry", "other-automation"],
)
def test_image_from_another_repository_is_refused(harness, test_engine, foreign_image):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]

    resp = harness.webhook(success(created["id"], sha, image=foreign_image))

    assert resp.status_code == 400
    assert resp.json()["errors"][0]["field"] == "image"
    assert harness.capp.calls == []
    assert row(test_engine, created["id"])["build_state"] == "building"


@pytest.mark.parametrize(
    "configured",
    ["", "Artifactory.test/automations", "artifactory.test/auto mations"],
    ids=["unset", "uppercase", "whitespace"],
)
def test_unusable_image_repository_fails_closed(harness, test_engine, monkeypatch, configured):
    # A prefix no image could ever match is misconfiguration (503, retryable),
    # not a foreign image (400) — and never a cutover.
    monkeypatch.setattr(config, "AUTOMATION_IMAGE_REPOSITORY", configured)
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    assert harness.webhook(success(created["id"], sha)).status_code == 503
    assert harness.capp.calls == []


def test_image_repository_trailing_slash_is_tolerated(harness, test_engine, monkeypatch):
    monkeypatch.setattr(config, "AUTOMATION_IMAGE_REPOSITORY", REPOSITORY + "/")
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    assert harness.webhook(success(created["id"], sha)).json() == {"status": "applied"}


# --- first build ------------------------------------------------------------

def test_first_build_success_deploys_and_cuts_over(harness, test_engine):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    built = image(created["id"])

    resp = harness.webhook(success(created["id"], sha))

    assert resp.status_code == 200
    assert resp.json() == {"status": "applied"}
    assert harness.capp.calls == [
        ("create", created["id"], "payments-wallet", built, ("payments-secret",))
    ]
    automation = row(test_engine, created["id"])
    assert automation["active_digest"] == built
    assert automation["contract_version"] == "1"
    assert automation["capp_deployment_id"] == f"automation-{created['id']}"
    assert automation["deployment_url"].endswith(created["id"])
    assert automation["build_state"] == "idle"
    assert automation["building_sha"] is None
    assert automation["build_lock_deadline"] is None
    # First build lands inactive — "ready to enable", never auto-armed.
    assert automation["matching_state"] == "inactive"
    # The create learns its digest; the cutover itself is audited (spec §8.4).
    assert revisions(test_engine, created["id"]) == [
        {"action": "create", "git_sha": sha, "resulting_digest": built,
         "actor": "noauth@keep"},
        {"action": "build_succeeded", "git_sha": sha, "resulting_digest": built,
         "actor": "ci-webhook"},
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
            "active_digest": built,
        }
    ]


def test_first_build_without_secret_sends_no_secret_refs(harness, test_engine):
    created = harness.create(secret_name=None)
    sha = row(test_engine, created["id"])["building_sha"]
    harness.webhook(success(created["id"], sha))
    assert harness.capp.calls[0][-1] == ()


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
    # Audited (spec §8.4): the next edit overwrites `build_failed`, this row stays.
    assert revisions(test_engine, created["id"])[-1] == {
        "action": "build_failed", "git_sha": sha, "resulting_digest": None,
        "actor": "ci-webhook",
    }
    assert harness.events.events == [
        {
            "type": "build",
            "automation_id": created["id"],
            "tenant_id": TENANT,
            "commit_sha": sha,
            "build_state": "build_failed",
            "matching_state": "inactive",
            "active_digest": None,
        }
    ]


@pytest.mark.parametrize("outcome", ["success", "failed"])
def test_build_event_carries_the_automations_own_tenant(harness, test_engine, outcome):
    # D19 routes the event to this tenant's sessions only, so it must come from
    # the row — never a default or the noauth tenant.
    harness.act_as(OTHER_TENANT)
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    payload = success(created["id"], sha) if outcome == "success" else failed(created["id"], sha)

    assert harness.webhook(payload).json() == {"status": "applied"}
    assert [e["tenant_id"] for e in harness.events.events] == [OTHER_TENANT]


def test_retry_after_failed_first_build_creates_the_deployment(harness, test_engine):
    created = harness.create()
    harness.webhook(failed(created["id"], row(test_engine, created["id"])["building_sha"]))

    # The author retries with an edit: the mid-build 409 no longer applies,
    # and the lock is re-armed for the new commit.
    harness.edit(created["id"])
    automation = row(test_engine, created["id"])
    assert_lock_armed(automation)
    sha = automation["building_sha"]
    assert sha == revisions(test_engine, created["id"])[-1]["git_sha"]

    assert harness.webhook(success(created["id"], sha)).json() == {"status": "applied"}
    # Still no deployment existed, so this build creates it (not a rollout).
    assert [c[0] for c in harness.capp.calls] == ["create"]
    assert row(test_engine, created["id"])["capp_deployment_id"] == f"automation-{created['id']}"


# --- edits --------------------------------------------------------------------

def _built(harness, test_engine) -> str:
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    assert harness.webhook(success(created["id"], sha)).status_code == 200
    return created["id"]


def test_edit_arms_build_lock_with_new_sha(harness, test_engine):
    automation_id = _built(harness, test_engine)
    harness.edit(automation_id)
    automation = row(test_engine, automation_id)
    assert_lock_armed(automation)
    assert automation["building_sha"] == revisions(test_engine, automation_id)[-1]["git_sha"]


def test_edit_build_success_rolls_out_existing_deployment(harness, test_engine):
    automation_id = _built(harness, test_engine)
    harness.edit(automation_id)
    sha = row(test_engine, automation_id)["building_sha"]
    new_image = image(automation_id, 2)

    resp = harness.webhook(success(automation_id, sha, 2, contract_version="2"))

    assert resp.json() == {"status": "applied"}
    assert harness.capp.calls[-1] == (
        "rollout", f"automation-{automation_id}", new_image, ("payments-secret",)
    )
    assert len([c for c in harness.capp.calls if c[0] == "create"]) == 1
    automation = row(test_engine, automation_id)
    assert automation["active_digest"] == new_image
    assert automation["contract_version"] == "2"
    assert automation["capp_deployment_id"] == f"automation-{automation_id}"
    assert automation["build_state"] == "idle"
    # Only the edit's revision learns the new digest; the create's keeps v1.
    create_sha = revisions(test_engine, automation_id)[0]["git_sha"]
    assert revisions(test_engine, automation_id) == [
        {"action": "create", "git_sha": create_sha, "resulting_digest": image(automation_id),
         "actor": "noauth@keep"},
        {"action": "build_succeeded", "git_sha": create_sha,
         "resulting_digest": image(automation_id), "actor": "ci-webhook"},
        {"action": "edit", "git_sha": sha, "resulting_digest": new_image,
         "actor": "noauth@keep"},
        {"action": "build_succeeded", "git_sha": sha, "resulting_digest": new_image,
         "actor": "ci-webhook"},
    ]
    assert harness.reload.count == 2
    assert harness.events.events[-1]["active_digest"] == new_image
    assert harness.events.events[-1]["tenant_id"] == TENANT


def test_rollout_mounts_the_edited_secret(harness, test_engine):
    automation_id = _built(harness, test_engine)
    harness.edit(automation_id, secret_name="rotated-secret")
    sha = row(test_engine, automation_id)["building_sha"]

    harness.webhook(success(automation_id, sha, 2))

    assert harness.capp.calls[-1] == (
        "rollout", f"automation-{automation_id}", image(automation_id, 2), ("rotated-secret",)
    )


def test_edit_cannot_move_a_deployed_automation_to_another_wallet(harness, test_engine):
    # Rollout only swaps image + secrets; accepting a wallet change would leave
    # the deployment running in the old wallet while the row names the new one.
    automation_id = _built(harness, test_engine)
    before = row(test_engine, automation_id)
    revisions_before = revisions(test_engine, automation_id)

    resp = harness.edit(automation_id, expect=400, namespace="other-wallet")

    # The golden fixture is the shape shared with the UI.
    golden = json.loads((FIXTURES / "namespace_immutable_error.json").read_text())
    assert resp.json() == golden
    assert row(test_engine, automation_id) == before
    assert revisions(test_engine, automation_id) == revisions_before


def test_wallet_may_change_until_the_first_deployment(harness, test_engine):
    created = harness.create()
    harness.webhook(failed(created["id"], row(test_engine, created["id"])["building_sha"]))

    harness.edit(created["id"], namespace="other-wallet")
    sha = row(test_engine, created["id"])["building_sha"]
    harness.webhook(success(created["id"], sha))

    assert harness.capp.calls == [
        ("create", created["id"], "other-wallet", image(created["id"]), ("payments-secret",))
    ]


def test_edit_of_active_automation_keeps_it_active(harness, test_engine):
    automation_id = _built(harness, test_engine)
    with test_engine.begin() as conn:
        conn.execute(
            text("UPDATE automations SET matching_state = 'active' WHERE id = :id"),
            {"id": automation_id},
        )
    harness.edit(automation_id)
    sha = row(test_engine, automation_id)["building_sha"]
    harness.webhook(success(automation_id, sha, 2))
    automation = row(test_engine, automation_id)
    assert automation["matching_state"] == "active"
    assert automation["active_digest"] == image(automation_id, 2)
    assert harness.events.events[-1]["matching_state"] == "active"


def test_failed_edit_build_keeps_old_digest_serving(harness, test_engine):
    automation_id = _built(harness, test_engine)
    calls_before = list(harness.capp.calls)
    harness.edit(automation_id)
    sha = row(test_engine, automation_id)["building_sha"]

    resp = harness.webhook(failed(automation_id, sha))

    assert resp.json() == {"status": "applied"}
    automation = row(test_engine, automation_id)
    assert automation["build_state"] == "build_failed"
    assert automation["active_digest"] == image(automation_id)
    assert harness.capp.calls == calls_before
    assert harness.capp.images[f"automation-{automation_id}"] == image(automation_id)


# --- CAPP failure -------------------------------------------------------------

def test_capp_failure_is_503_logged_and_leaves_build_locked(harness, test_engine, caplog):
    created = harness.create()
    before = row(test_engine, created["id"])
    harness.capp.fail = True

    with caplog.at_level(logging.WARNING):
        resp = harness.webhook(success(created["id"], before["building_sha"]))

    assert resp.status_code == 503
    assert "CAPP" not in resp.text
    assert f"automation={created['id']}" in caplog.text
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
    assert row(test_engine, created["id"])["active_digest"] == image(created["id"])


def test_failed_rollout_keeps_old_digest(harness, test_engine):
    automation_id = _built(harness, test_engine)
    harness.edit(automation_id)
    before = row(test_engine, automation_id)
    harness.capp.fail = True
    _, reloads, events = harness.counts()

    assert harness.webhook(success(automation_id, before["building_sha"], 2)).status_code == 503
    automation = row(test_engine, automation_id)
    for column in ("active_digest", "build_state", "building_sha", "build_lock_deadline",
                   "contract_version"):
        assert automation[column] == before[column]
    assert automation["active_digest"] == image(automation_id)
    assert harness.capp.images[f"automation-{automation_id}"] == image(automation_id)
    assert harness.reload.count == reloads
    assert len(harness.events.events) == events


def test_production_default_capp_client_fails_closed(test_engine):
    # Until D16 the real client is absent: the webhook must 503, never cut over
    # to an image no deployment serves.
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
    before = harness.counts()

    resp = harness.webhook(success(created["id"], sha))

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "not_building"}
    assert row(test_engine, created["id"]) == state
    assert harness.counts() == before


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
    resp = harness.webhook(success(automation_id, first_sha, 2))

    assert resp.json() == {"status": "ignored", "reason": "sha_mismatch"}
    automation = row(test_engine, automation_id)
    assert automation["build_state"] == "building"
    assert automation["active_digest"] == image(automation_id)
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
    """Records pooled connections checked out during each call; can run a
    side effect mid-call to simulate a concurrent actor."""

    def __init__(self, engine, during_call=None):
        super().__init__()
        self.engine = engine
        self.during_call = during_call
        self.checked_out: list[int] = []

    def create_deployment(self, **kwargs):
        self.checked_out.append(self.engine.pool.checkedout())
        if self.during_call is not None:
            self.during_call()
        return super().create_deployment(**kwargs)


def _event(automation_id: str, sha: str) -> CiWebhookIn:
    return CiWebhookIn(**success(automation_id, sha))


def test_no_db_connection_is_held_across_the_capp_call(harness, test_engine):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    capp = _ProbingCapp(test_engine)
    builds.apply_build_result(_event(created["id"], sha), capp, RecordingReload(), RecordingEvents())
    assert capp.checked_out == [0]


@pytest.mark.parametrize("outcome", ["success", "failed"])
def test_commit_rechecks_admission_under_a_row_lock(harness, test_engine, outcome):
    # The race tests run sequentially, so they pass without the lock; pin the
    # FOR UPDATE itself — it is what stops two deliveries both cutting over.
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    payload = success(created["id"], sha) if outcome == "success" else failed(created["id"], sha)
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(test_engine, "before_cursor_execute", record)
    try:
        builds.apply_build_result(
            CiWebhookIn(**payload), InMemoryCappClient(), RecordingReload(), RecordingEvents()
        )
    finally:
        event.remove(test_engine, "before_cursor_execute", record)

    locked = [s for s in statements if "FROM automations" in s and "FOR UPDATE" in s]
    assert len(locked) == 1


def test_unstored_create_then_wallet_edit_redeploys_in_the_new_wallet(harness, test_engine):
    # Drift path, as it really happens: while CAPP creates the first
    # deployment, a same-commit `failed` lands and the author edits — moving
    # the wallet, allowed since the row has no deployment yet. The success then
    # loses to the newer edit (sha_mismatch), so the deployment's id is never
    # stored. The edit's own build must not land on the old-wallet deployment.
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]

    def fail_then_move_wallet():
        builds.apply_build_result(
            CiWebhookIn(**failed(created["id"], sha)),
            InMemoryCappClient(),
            RecordingReload(),
            RecordingEvents(),
        )
        harness.edit(created["id"], namespace="other-wallet")

    probe = _ProbingCapp(test_engine, fail_then_move_wallet)
    result = builds.apply_build_result(
        CiWebhookIn(**success(created["id"], sha)), probe, RecordingReload(), RecordingEvents()
    )
    assert (result.status, result.reason) == ("ignored", "sha_mismatch")
    assert row(test_engine, created["id"])["capp_deployment_id"] is None
    assert probe.wallets[created["id"]] == "payments-wallet"
    old_deployment = probe.deployments[created["id"]].deployment_id

    probe.during_call = None
    harness.client.app.dependency_overrides[get_capp_client] = lambda: probe
    sha = row(test_engine, created["id"])["building_sha"]
    calls_before = len(probe.calls)
    assert harness.webhook(success(created["id"], sha)).json() == {"status": "applied"}

    # A create in the new wallet that tears the old deployment down — no rollout.
    assert probe.calls[calls_before:] == [
        ("create", created["id"], "other-wallet", image(created["id"]), ("payments-secret",)),
        ("teardown", old_deployment),
    ]
    # Same pinned name (`automation-{id}`), now in the new wallet; the old-wallet
    # deployment was torn down first (the teardown call above).
    recreated = probe.deployments[created["id"]]
    assert recreated.deployment_id == f"automation-{created['id']}"
    assert probe.wallets[created["id"]] == "other-wallet"
    automation = row(test_engine, created["id"])
    assert automation["capp_deployment_id"] == recreated.deployment_id
    assert automation["deployment_url"] == recreated.deployment_url
    assert automation["deployment_url"].startswith("https://other-wallet.")


class _HookedGit(InMemoryGitClient):
    """Runs a side effect inside the commit — another actor mid-PUT."""

    def __init__(self, during_commit):
        super().__init__()
        self.during_commit = during_commit

    def commit_script(self, *args, **kwargs):
        self.during_commit()
        return super().commit_script(*args, **kwargs)


def test_deploy_during_a_wallet_edit_supersedes_it(harness, test_engine):
    # The wallet is checked once, at the claim (no deployment yet). Anything
    # that deploys the row before the edit finishes must first take the claim
    # away, so the edit is superseded and the wallet move is never written.
    created = harness.create()
    harness.webhook(failed(created["id"], row(test_engine, created["id"])["building_sha"]))
    revisions_before = revisions(test_engine, created["id"])

    # The real interleaving's end state: a concurrent edit's build finished,
    # leaving the row idle with a deployment.
    def deploy_meanwhile():
        with test_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE automations SET capp_deployment_id = 'capp-x', "
                    "build_state = 'idle' WHERE id = :id"
                ),
                {"id": created["id"]},
            )

    git = _HookedGit(deploy_meanwhile)
    harness.client.app.dependency_overrides[get_git_client] = lambda: git
    harness.edit(created["id"], expect=409, namespace="other-wallet")

    automation = row(test_engine, created["id"])
    assert automation["namespace"] == "payments-wallet"
    assert automation["build_state"] == "idle"
    assert revisions(test_engine, created["id"]) == revisions_before


def test_busy_row_at_commit_is_a_bounded_503_and_redeliverable(
    harness, test_engine, monkeypatch
):
    # The cutover takes the same bounded lock as edit/toggle/DELETE admission:
    # a row held elsewhere is a retryable 503, never a statement-timeout 500.
    monkeypatch.setattr(config, "DB_LOCK_TIMEOUT_MS", 200)
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    held, release = threading.Event(), threading.Event()

    def holder():
        with test_engine.connect() as conn, conn.begin():
            conn.execute(text("SELECT id FROM automations FOR UPDATE"))
            held.set()
            release.wait(timeout=15)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(timeout=10)
        started = time.monotonic()
        resp = harness.webhook(success(created["id"], sha))
        elapsed = time.monotonic() - started
    finally:
        release.set()
        thread.join(timeout=15)

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "1"
    assert elapsed < 3
    assert row(test_engine, created["id"])["build_state"] == "building"
    # CAPP's create is idempotent by automation_id, so redelivery completes it.
    assert harness.webhook(success(created["id"], sha)).json() == {"status": "applied"}


def test_concurrent_duplicate_delivery_cuts_over_once(harness, test_engine, caplog):
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
    with caplog.at_level(logging.WARNING):
        result = builds.apply_build_result(
            _event(created["id"], sha), _ProbingCapp(test_engine, rival), reload, events
        )

    # A duplicate is harmless: no CAPP-ahead-of-row warning.
    assert "CAPP ahead of row" not in caplog.text
    assert result.status == "ignored"
    assert len(rival_events.events) == 1
    assert events.events == [] and reload.count == 0
    assert row(test_engine, created["id"])["active_digest"] == image(created["id"])


@pytest.mark.parametrize("first_build", [True, False], ids=["create", "rollout"])
def test_failure_for_the_same_commit_during_the_capp_call_does_not_strand_capp(
    harness, test_engine, caplog, first_build
):
    # A retried job, a second pipeline, or any token holder reports `failed`
    # for the commit whose success is mid-CAPP-call. CAPP has accepted the
    # image, so the row must end up recording what CAPP serves — not
    # `build_failed` with the old digest (or, on a first build, a deployment
    # whose id was never stored).
    if first_build:
        created = harness.create()
        automation_id = created["id"]
    else:
        automation_id = _built(harness, test_engine)
        harness.edit(automation_id)
    sha = row(test_engine, automation_id)["building_sha"]
    version = 1 if first_build else 2

    def report_failure():
        result = builds.apply_build_result(
            CiWebhookIn(**failed(automation_id, sha)),
            InMemoryCappClient(),
            RecordingReload(),
            RecordingEvents(),
        )
        assert result.status == "applied"

    capp = _ProbingCapp(test_engine, report_failure)
    if not first_build:
        capp.deployments[automation_id] = harness.capp.deployments[automation_id]
        capp.wallets[automation_id] = harness.capp.wallets[automation_id]
        capp.rollout = _with_hook(capp.rollout, report_failure)
        capp.during_call = None
    events = RecordingEvents()
    with caplog.at_level(logging.WARNING):
        result = builds.apply_build_result(
            CiWebhookIn(**success(automation_id, sha, version)),
            capp,
            RecordingReload(),
            events,
        )

    assert result.status == "applied"
    assert "CAPP ahead of row" not in caplog.text
    automation = row(test_engine, automation_id)
    assert automation["build_state"] == "idle"
    assert automation["active_digest"] == image(automation_id, version)
    assert automation["capp_deployment_id"] == capp.deployments[automation_id].deployment_id
    assert events.events[-1]["build_state"] == "idle"
    # Both transitions are on the record, in order; the digest stamp for the
    # requesting revision never lands on the failure row.
    outcomes = [r for r in revisions(test_engine, automation_id) if r["git_sha"] == sha]
    assert [(r["action"], r["resulting_digest"]) for r in outcomes] == [
        ("create" if first_build else "edit", image(automation_id, version)),
        ("build_failed", None),
        ("build_succeeded", image(automation_id, version)),
    ]


def test_success_never_overrides_an_edit_made_after_the_raced_failure(
    harness, test_engine, caplog
):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]

    def fail_then_edit():
        builds.apply_build_result(
            CiWebhookIn(**failed(created["id"], sha)),
            InMemoryCappClient(),
            RecordingReload(),
            RecordingEvents(),
        )
        harness.edit(created["id"])

    with caplog.at_level(logging.WARNING):
        result = builds.apply_build_result(
            CiWebhookIn(**success(created["id"], sha)),
            _ProbingCapp(test_engine, fail_then_edit),
            RecordingReload(),
            RecordingEvents(),
        )

    assert (result.status, result.reason) == ("ignored", "sha_mismatch")
    automation = row(test_engine, created["id"])
    assert automation["build_state"] == "building"
    assert automation["building_sha"] == revisions(test_engine, created["id"])[-1]["git_sha"]
    assert automation["active_digest"] is None
    assert "CAPP ahead of row" in caplog.text


def _report_failure(automation_id: str, sha: str) -> None:
    builds.apply_build_result(
        CiWebhookIn(**failed(automation_id, sha)),
        InMemoryCappClient(),
        RecordingReload(),
        RecordingEvents(),
    )


def test_success_never_overrides_a_newer_edit_whose_build_already_failed(
    harness, test_engine
):
    # The newer edit's own build failed fast, so the row is `build_failed`
    # again — but for the newer commit. The raced success must not cut over.
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]

    def fail_edit_fail():
        _report_failure(created["id"], sha)
        harness.edit(created["id"])
        _report_failure(created["id"], row(test_engine, created["id"])["building_sha"])

    result = builds.apply_build_result(
        CiWebhookIn(**success(created["id"], sha)),
        _ProbingCapp(test_engine, fail_edit_fail),
        RecordingReload(),
        RecordingEvents(),
    )

    assert (result.status, result.reason) == ("ignored", "not_building")
    automation = row(test_engine, created["id"])
    assert automation["build_state"] == "build_failed"
    assert automation["active_digest"] is None
    assert automation["capp_deployment_id"] is None


def test_raced_failure_still_reconciles_after_a_toggle_revision(harness, test_engine):
    # Only create/edit revisions say which commit the row is building; a newer
    # enable/disable row (no git_sha) must not hide the raced failure.
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]

    def fail_then_toggle():
        _report_failure(created["id"], sha)
        with test_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO automation_revisions "
                    "(id, automation_id, action, actor, created_at) "
                    "VALUES (gen_random_uuid(), :id, 'disable', 'someone', now())"
                ),
                {"id": created["id"]},
            )

    result = builds.apply_build_result(
        CiWebhookIn(**success(created["id"], sha)),
        _ProbingCapp(test_engine, fail_then_toggle),
        RecordingReload(),
        RecordingEvents(),
    )

    assert result.status == "applied"
    assert row(test_engine, created["id"])["active_digest"] == image(created["id"])


def test_redelivery_after_a_raced_failure_and_a_503_completes_the_cutover(
    harness, test_engine, monkeypatch
):
    # A raced failure leaves the row `build_failed`; the cutover then fails to
    # be recorded (503, "redeliver"). The redelivery must finish it, not stop
    # at the first admission check on `not_building`.
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    real_lock_first = builds.lock_first

    def broken(session, query):
        raise OperationalError("SELECT ... FOR UPDATE", {}, Exception("connection lost"))

    def fail_then_break_the_db():
        _report_failure(created["id"], sha)
        monkeypatch.setattr(builds, "lock_first", broken)

    capp = _ProbingCapp(test_engine, fail_then_break_the_db)
    harness.client.app.dependency_overrides[get_capp_client] = lambda: capp
    assert harness.webhook(success(created["id"], sha)).status_code == 503
    assert row(test_engine, created["id"])["build_state"] == "build_failed"

    monkeypatch.setattr(builds, "lock_first", real_lock_first)
    capp.during_call = None
    assert harness.webhook(success(created["id"], sha)).json() == {"status": "applied"}
    automation = row(test_engine, created["id"])
    assert automation["build_state"] == "idle"
    assert automation["active_digest"] == image(created["id"])
    assert automation["capp_deployment_id"] == f"automation-{created['id']}"


def test_teardown_uses_the_wallet_the_deployment_was_created_in(harness, test_engine):
    # Raced failure → wallet-moving edit → that edit's build fails → DELETE,
    # all while the first create is inside CAPP. The row now names the new
    # wallet; the deployment lives in the old one, so that is where it goes.
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]

    def race_to_deletion():
        _report_failure(created["id"], sha)
        harness.edit(created["id"], namespace="other-wallet")
        _report_failure(created["id"], row(test_engine, created["id"])["building_sha"])
        with test_engine.begin() as conn:
            conn.execute(
                text("UPDATE automations SET matching_state = 'deleting' WHERE id = :id"),
                {"id": created["id"]},
            )

    deletion = RecordingDeletion()
    result = builds.apply_build_result(
        CiWebhookIn(**success(created["id"], sha)),
        _ProbingCapp(test_engine, race_to_deletion),
        RecordingReload(),
        RecordingEvents(),
        deletion,
    )

    assert result.reason == "deleting"
    assert row(test_engine, created["id"])["namespace"] == "other-wallet"
    assert {call[1] for call in deletion.calls} == {"payments-wallet"}


def test_a_failure_after_the_commit_is_not_reported_as_not_recorded(
    harness, test_engine, monkeypatch
):
    # Once the cutover commits, nothing may turn the answer into "not
    # recorded" — a post-commit read failing would send operators chasing
    # drift that does not exist and skip the reload/event.
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]

    def refresh_fails(self, *args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr(Session, "refresh", refresh_fails)
    resp = harness.webhook(success(created["id"], sha))

    assert resp.json() == {"status": "applied"}
    assert harness.reload.count == 1
    assert harness.events.events[-1]["build_state"] == "idle"


def _with_hook(call, hook):
    def wrapped(**kwargs):
        hook()
        return call(**kwargs)

    return wrapped


def test_deletion_started_during_capp_call_is_not_cut_over(harness, test_engine, caplog):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]

    def start_deletion():
        with test_engine.begin() as conn:
            conn.execute(
                text("UPDATE automations SET matching_state = 'deleting' WHERE id = :id"),
                {"id": created["id"]},
            )

    events = RecordingEvents()
    reload = RecordingReload()
    capp = _ProbingCapp(test_engine, start_deletion)
    deletion = RecordingDeletion()
    with caplog.at_level(logging.WARNING):
        result = builds.apply_build_result(
            _event(created["id"], sha), capp, reload, events, deletion
        )

    assert (result.status, result.reason) == ("ignored", "deleting")
    automation = row(test_engine, created["id"])
    assert automation["active_digest"] is None
    assert automation["capp_deployment_id"] is None
    assert events.events == [] and reload.count == 0
    assert "CAPP ahead of row" in caplog.text
    assert "reason=deleting" in caplog.text
    # The cascade's CAPP step may already have found nothing (404) and never
    # runs again, so the create is undone here — the same two deletions, in
    # the wallet it was created in.
    assert deletion.calls == [
        ("delete_capp", "payments-wallet", f"automation-{created['id']}"),
        ("delete_secret", "payments-wallet", f"automation-{created['id']}-run-auth"),
    ]


def test_teardown_failure_is_logged_and_still_ignored(harness, test_engine, caplog):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]

    def start_deletion():
        with test_engine.begin() as conn:
            conn.execute(
                text("UPDATE automations SET matching_state = 'deleting' WHERE id = :id"),
                {"id": created["id"]},
            )

    with caplog.at_level(logging.WARNING):
        # The production default deletion client is unconfigured until D16.
        result = builds.apply_build_result(
            _event(created["id"], sha),
            _ProbingCapp(test_engine, start_deletion),
            RecordingReload(),
            RecordingEvents(),
        )

    assert (result.status, result.reason) == ("ignored", "deleting")
    assert "could not tear down" in caplog.text
    assert "ExternalDependencyNotConfiguredError" in caplog.text


def test_rollout_losing_to_deletion_tears_nothing_down(harness, test_engine):
    # The deployment's id is stored, so the cascade owns its removal.
    automation_id = _built(harness, test_engine)
    harness.edit(automation_id)
    sha = row(test_engine, automation_id)["building_sha"]

    def start_deletion():
        with test_engine.begin() as conn:
            conn.execute(
                text("UPDATE automations SET matching_state = 'deleting' WHERE id = :id"),
                {"id": automation_id},
            )

    capp = InMemoryCappClient()
    capp.deployments[automation_id] = harness.capp.deployments[automation_id]
    capp.wallets[automation_id] = harness.capp.wallets[automation_id]
    capp.rollout = _with_hook(capp.rollout, start_deletion)
    deletion = RecordingDeletion()
    result = builds.apply_build_result(
        CiWebhookIn(**success(automation_id, sha, 2)),
        capp,
        RecordingReload(),
        RecordingEvents(),
        deletion,
    )

    assert result.reason == "deleting"
    assert deletion.calls == []


def test_db_failure_after_capp_accepted_is_a_redeliverable_503(
    harness, test_engine, monkeypatch, caplog
):
    created = harness.create()
    sha = row(test_engine, created["id"])["building_sha"]
    real_lock_first = builds.lock_first

    def broken(session, query):
        raise OperationalError("SELECT ... FOR UPDATE", {}, Exception("connection lost"))

    monkeypatch.setattr(builds, "lock_first", broken)
    with caplog.at_level(logging.WARNING):
        resp = harness.webhook(success(created["id"], sha))

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "1"
    assert "connection lost" not in resp.text
    assert "could not be recorded (OperationalError)" in caplog.text
    assert "connection lost" not in caplog.text
    assert row(test_engine, created["id"])["build_state"] == "building"

    monkeypatch.setattr(builds, "lock_first", real_lock_first)
    assert harness.webhook(success(created["id"], sha)).json() == {"status": "applied"}
