"""Automation state-event publisher: JSON on the configured channel, bounded,
never raises (D15 — the SSE source D19 streams)."""
import json
import logging

from src.core import automation_events as events_module
from src.core.automation_events import (
    NullAutomationEventPublisher,
    RedisAutomationEventPublisher,
)


class FakeRedis:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.published: list[tuple[str, str]] = []

    def publish(self, channel, message):
        if self.fail:
            raise self.fail
        self.published.append((channel, message))


def test_publishes_event_as_json_on_configured_channel():
    fake = FakeRedis()
    publisher = RedisAutomationEventPublisher(
        "redis://x", "automation-events", 1.0, client_factory=lambda: fake
    )
    event = {"type": "build", "automation_id": "a", "tenant_id": "keep"}
    publisher.publish(event)
    [(channel, message)] = fake.published
    assert channel == "automation-events"
    assert json.loads(message) == event


def test_publish_failure_is_swallowed_without_leaking_the_url(caplog):
    publisher = RedisAutomationEventPublisher(
        "redis://:hunter2@redis:6379",
        "automation-events",
        1.0,
        client_factory=lambda: FakeRedis(fail=ConnectionError("redis://:hunter2@redis refused")),
    )
    with caplog.at_level(logging.WARNING):
        publisher.publish({"type": "build"})  # must not raise
    assert "ConnectionError" in caplog.text
    assert "hunter2" not in caplog.text


def test_default_is_null_publisher_without_redis_url(monkeypatch):
    monkeypatch.setattr(events_module.config, "REDIS_URL", "")
    monkeypatch.setattr(events_module, "_default_publisher", None)
    assert isinstance(events_module.get_default_event_publisher(), NullAutomationEventPublisher)
    NullAutomationEventPublisher().publish({"type": "build"})


def test_default_is_redis_publisher_with_redis_url(monkeypatch):
    monkeypatch.setattr(events_module.config, "REDIS_URL", "redis://localhost:6379")
    monkeypatch.setattr(events_module, "_default_publisher", None)
    assert isinstance(
        events_module.get_default_event_publisher(), RedisAutomationEventPublisher
    )
