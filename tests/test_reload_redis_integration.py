"""Opt-in: the publisher against a real Redis, as keep-event-handler subscribes.

Skipped unless TEST_REDIS_URL is set (CI has no Redis). Locally:
`TEST_REDIS_URL=redis://localhost:6379 poetry run pytest tests/test_reload_redis_integration.py`
against the root docker-compose stack. This proves channel/wire compatibility
with B4's subscriber (default channel `reload`); it does not stand up the
event-handler's reload worker — that end-to-end check lives with B4.
"""
import os
import time
import uuid

import pytest

from src.core.reload import RELOAD_MESSAGE, RedisReloadPublisher

REDIS_URL = os.environ.get("TEST_REDIS_URL")

pytestmark = pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL not set")


def test_subscriber_on_reload_channel_receives_the_signal():
    import redis

    channel = f"reload-test-{uuid.uuid4()}"
    client = redis.Redis.from_url(REDIS_URL, socket_timeout=2)
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(channel)
    try:
        # Drain the subscribe confirmation before publishing.
        pubsub.get_message(timeout=1)
        RedisReloadPublisher(REDIS_URL, channel, 1.0).publish_reload()

        deadline = time.monotonic() + 5
        message = None
        while message is None and time.monotonic() < deadline:
            message = pubsub.get_message(timeout=0.5)
        assert message is not None, "no reload signal received"
        assert message["channel"].decode() == channel
        assert message["data"].decode() == RELOAD_MESSAGE
    finally:
        pubsub.close()
        client.close()


def test_unreachable_redis_is_bounded_and_swallowed():
    publisher = RedisReloadPublisher("redis://10.255.255.1:6379", "reload", 0.5)
    started = time.monotonic()
    publisher.publish_reload()
    assert time.monotonic() - started < 5
