"""Redis `reload` publisher: signal-only, bounded, never raises (D18 / spec §4.5)."""
import logging

from src.core import reload as reload_module
from src.core.reload import (
    RELOAD_MESSAGE,
    NullReloadPublisher,
    RedisReloadPublisher,
)


class FakeRedis:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.published: list[tuple[str, str]] = []

    def publish(self, channel, message):
        if self.fail:
            raise self.fail
        self.published.append((channel, message))


def test_publishes_signal_token_on_configured_channel():
    fake = FakeRedis()
    publisher = RedisReloadPublisher("redis://x", "reload", 1.0, client_factory=lambda: fake)
    publisher.publish_reload()
    assert fake.published == [("reload", RELOAD_MESSAGE)]


def test_client_built_once_for_process_lifetime():
    built = []

    def factory():
        built.append(1)
        return FakeRedis()

    publisher = RedisReloadPublisher("redis://x", "reload", 1.0, client_factory=factory)
    for _ in range(3):
        publisher.publish_reload()
    assert len(built) == 1


def test_publish_failure_is_swallowed_and_does_not_log_the_error_text(caplog):
    secret_url_error = ConnectionError("redis://:hunter2@redis:6379 refused")
    publisher = RedisReloadPublisher(
        "redis://:hunter2@redis:6379",
        "reload",
        1.0,
        client_factory=lambda: FakeRedis(fail=secret_url_error),
    )
    with caplog.at_level(logging.WARNING):
        publisher.publish_reload()  # must not raise
    assert "ConnectionError" in caplog.text
    assert "hunter2" not in caplog.text


def test_default_is_null_publisher_without_redis_url(monkeypatch):
    monkeypatch.setattr(reload_module.config, "REDIS_URL", "")
    monkeypatch.setattr(reload_module, "_default_publisher", None)
    assert isinstance(reload_module.get_default_reload_publisher(), NullReloadPublisher)
    NullReloadPublisher().publish_reload()


def test_default_is_redis_publisher_with_redis_url(monkeypatch):
    monkeypatch.setattr(reload_module.config, "REDIS_URL", "redis://localhost:6379")
    monkeypatch.setattr(reload_module, "_default_publisher", None)
    assert isinstance(reload_module.get_default_reload_publisher(), RedisReloadPublisher)
