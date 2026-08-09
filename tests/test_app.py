"""Basic skeleton tests: the app boots and each tier router is mounted.

Uses the conftest `client` fixture — GET /automations is DB-backed since D13,
so the app must run against the test engine (injected by `test_engine`).
"""


def test_root_ok(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "keep-automation-api"


def test_healthcheck_ok(client):
    resp = client.get("/healthcheck")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_user_tier_mounted(client):
    assert client.get("/automations").status_code == 200
    assert client.get("/namespaces").status_code == 200
    assert client.get("/alert-schema/fields").status_code == 200


def test_machine_tier_mounted(client):
    assert client.post("/internal/ci-webhook").status_code == 200


def test_internal_tier_mounted(client):
    assert client.post("/internal/submit").status_code == 200


def test_events_sse_mounted(client):
    resp = client.get("/events")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
