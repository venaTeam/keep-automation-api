"""Publisher for the automations `reload` invalidation signal (spec §4.5, §5.3).

The channel carries a **signal, never data** (automation-contracts.md §"Redis
keys" → Invalidation): keep-event-handler's subscriber never reads the payload,
it only triggers a reload from the DB. So the message body is a fixed token and
must never grow a definition payload.

Publishing is **best-effort and after commit**. A lost publish is tolerated by
design — the subscriber's unconditional periodic reload and reload-on-reconnect
converge the index anyway (spec §6.1 #2) — so a failure is logged and swallowed,
never raised back into a transition that has already committed.

One client for the process lifetime: `Redis.from_url()` builds a new connection
pool per call, and rebuilding it per publish leaks descriptors (the failure
keep-event-handler's `pubsub.py` documents).
"""
import logging
import threading
from typing import Callable, Protocol

from src import config

logger = logging.getLogger(__name__)

RELOAD_MESSAGE = "reload"


class ReloadPublisher(Protocol):
    def publish_reload(self) -> None:
        """Fire the signal. Must never raise."""
        ...


class NullReloadPublisher:
    """Used when REDIS_URL is unset: the periodic reload is the only propagation."""

    def publish_reload(self) -> None:
        logger.debug("automations: REDIS_URL unset -- reload publish skipped")


class RedisReloadPublisher:
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

    def publish_reload(self) -> None:
        try:
            self._get_client().publish(self._channel, RELOAD_MESSAGE)
        except Exception as exc:  # noqa: BLE001 — best-effort by contract
            # Type only: a redis error string can embed the URL, and with it
            # the password.
            logger.warning(
                "automations: reload publish failed (%s); the periodic reload "
                "will converge the index",
                type(exc).__name__,
            )


_default_publisher: ReloadPublisher | None = None
_default_publisher_lock = threading.Lock()


def get_default_reload_publisher() -> ReloadPublisher:
    """Process-wide publisher, built once under a lock: concurrent first
    requests from the threadpool must not each build a client and pool."""
    global _default_publisher
    with _default_publisher_lock:
        if _default_publisher is None:
            if config.REDIS_URL:
                _default_publisher = RedisReloadPublisher(
                    config.REDIS_URL,
                    config.AUTOMATION_RELOAD_CHANNEL,
                    config.REDIS_PUBLISH_TIMEOUT_SECONDS,
                )
            else:
                _default_publisher = NullReloadPublisher()
        return _default_publisher
