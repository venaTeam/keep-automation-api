"""Publisher for automation state events — the source of the `GET /events` SSE
stream (spec §5.1 "→ SSE to UI", §8.1).

Events go out on a Redis pub/sub channel, not an in-process queue: under
`gunicorn -w 4` the worker that applies a CI webhook is rarely the worker
holding a browser's SSE connection, so only a shared bus reaches it. D19's
stream subscribes and forwards each event to the sessions of the event's
`tenant_id` — every event carries it, and a subscriber must never forward an
event across tenants.

Same delivery posture as `reload` (src/core/reload.py): **best-effort, after
commit, never raises**. An SSE event is a UI nudge; the row is the truth and a
reconnecting UI re-reads it. Shape: automation-contracts.md §"CI webhook".
"""
import json
import logging
import threading
from typing import Callable, Protocol

from src import config

logger = logging.getLogger(__name__)


class AutomationEventPublisher(Protocol):
    def publish(self, event: dict) -> None:
        """Fire one event. Must never raise."""
        ...


class NullAutomationEventPublisher:
    """Used when REDIS_URL is unset: no live push, the UI sees state on read."""

    def publish(self, event: dict) -> None:
        logger.debug("automations: REDIS_URL unset -- event publish skipped")


class RedisAutomationEventPublisher:
    def __init__(
        self,
        url: str,
        channel: str,
        timeout_seconds: float,
        client_factory: Callable[[], object] | None = None,
    ):
        self._url = url
        self._channel = channel
        self._timeout = timeout_seconds
        self._client_factory = client_factory
        self._client = None
        self._lock = threading.Lock()

    def _get_client(self):
        # One client per process: `Redis.from_url()` builds a pool per call.
        with self._lock:
            if self._client is None:
                if self._client_factory is not None:
                    self._client = self._client_factory()
                else:
                    import redis

                    self._client = redis.Redis.from_url(
                        self._url,
                        socket_timeout=self._timeout,
                        socket_connect_timeout=self._timeout,
                    )
            return self._client

    def publish(self, event: dict) -> None:
        try:
            self._get_client().publish(
                self._channel, json.dumps(event, separators=(",", ":"))
            )
        except Exception as exc:  # noqa: BLE001 — best-effort by contract
            # Type only: a redis error string can embed the URL and password.
            logger.warning(
                "automations: event publish failed (%s); the UI sees the state "
                "on its next read",
                type(exc).__name__,
            )


_default_publisher: AutomationEventPublisher | None = None
_default_publisher_lock = threading.Lock()


def get_default_event_publisher() -> AutomationEventPublisher:
    """Process-wide publisher, built once under a lock (see reload.py)."""
    global _default_publisher
    with _default_publisher_lock:
        if _default_publisher is None:
            if config.REDIS_URL:
                _default_publisher = RedisAutomationEventPublisher(
                    config.REDIS_URL,
                    config.AUTOMATION_EVENTS_CHANNEL,
                    config.REDIS_PUBLISH_TIMEOUT_SECONDS,
                )
            else:
                _default_publisher = NullAutomationEventPublisher()
        return _default_publisher
