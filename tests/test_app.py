"""Basic skeleton tests: the app boots and each tier router is mounted (D12)."""
from fastapi.testclient import TestClient

from src.main import get_app

client = TestClient(get_app())


def test_root_ok():
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "keep-automation-api"


def test_healthcheck_ok():
    resp = client.get("/healthcheck")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_user_tier_mounted():
    assert client.get("/automations").status_code == 200
    assert client.get("/namespaces").status_code == 200
    assert client.get("/alert-schema/fields").status_code == 200


def test_machine_tier_mounted():
    assert client.post("/internal/ci-webhook").status_code == 200


def test_internal_tier_mounted():
    assert client.post("/internal/submit").status_code == 200


def test_events_sse_mounted():
    resp = client.get("/events")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
