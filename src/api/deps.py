"""Tier auth dependencies (D12 skeleton — placeholders, no real verification).

- user tier  -> the existing identity provider. Only a noauth shim is wired now;
  the shared identity manager is vendored later (never a second auth stack, §10.2).
- machine tier -> GitLab secret token (`X-Gitlab-Token`, verified since D15).
- internal tier -> service token (verified in D17/D20).
"""
import hashlib
import hmac
import logging
import threading
import time

from fastapi import Header, HTTPException, status

from src import config
from src.bl.capp_client import CappClient, get_default_capp_client
from src.bl.git_client import GitClient, build_gitlab_client
from src.core.automation_events import (
    AutomationEventPublisher,
    get_default_event_publisher,
)
from src.core.reload import ReloadPublisher, get_default_reload_publisher
from src.models.api.identity import AuthenticatedEntity

logger = logging.getLogger(__name__)


def get_git_client() -> GitClient:
    """Script-repo client — a fresh real GitLab client per request.

    Not a process-wide singleton: python-gitlab holds a non-thread-safe
    requests.Session and the BL runs in a threadpool, so a shared client could
    interleave concurrent requests' commits/reads. Construction does no network
    I/O (the project handle is lazy), so per-request creation is cheap.

    `build_gitlab_client` refuses under the noauth shim and fails fast if GitLab
    is unconfigured, so a misprovisioned pod surfaces on the first create/get
    rather than silently dropping commits. Tests inject the in-memory stub via
    dependency_overrides, so this GitLab path never runs under pytest.
    """
    return build_gitlab_client()


def get_capp_client() -> CappClient:
    """CAPP deploy/rollout client. Fails closed until D16 wires the real one."""
    return get_default_capp_client()


def get_reload_publisher() -> ReloadPublisher:
    """Redis `reload` publisher; a no-op when REDIS_URL is unset."""
    return get_default_reload_publisher()


def get_event_publisher() -> AutomationEventPublisher:
    """Automation state events for the SSE stream; a no-op without REDIS_URL."""
    return get_default_event_publisher()


async def get_authenticated_entity() -> AuthenticatedEntity:
    """User-tier identity. Noauth shim returns a static entity for now.

    `tenant_id` here is the ONLY source of tenant scope for the user tier — the
    BL takes it as an explicit argument and no request body may supply it
    (spec §4.1/§8.1). When the shared identity manager replaces this shim it
    must keep returning an AuthenticatedEntity, which is what the routes and
    every tenant-scoped query downstream are typed against.
    """
    return AuthenticatedEntity(tenant_id="keep", email="noauth@keep")


class _LogThrottle:
    """Lets one record through per `interval_seconds`; counts the rest.

    Rejected webhooks are logged so a misconfiguration is visible, but the
    endpoint is reachable by anything that gets past the network restriction —
    one line per rejected request would let a caller flood the logs.
    """

    def __init__(self, interval_seconds: float):
        self._interval = interval_seconds
        self._last: float | None = None
        self._suppressed = 0
        self._lock = threading.Lock()

    def allow(self) -> int | None:
        """Records swallowed since the last one let through, or None to skip."""
        now = time.monotonic()
        with self._lock:
            if self._last is not None and now - self._last < self._interval:
                self._suppressed += 1
                return None
            suppressed, self._suppressed, self._last = self._suppressed, 0, now
            return suppressed


REJECTION_LOG_INTERVAL_SECONDS = 60.0
_unconfigured_log = _LogThrottle(REJECTION_LOG_INTERVAL_SECONDS)
_mismatch_log = _LogThrottle(REJECTION_LOG_INTERVAL_SECONDS)


def ci_webhook_token_valid(x_gitlab_token: str | None) -> bool:
    """True when `x_gitlab_token` is the configured CI webhook secret.

    Fails closed: with `CI_WEBHOOK_TOKEN` unset nothing is valid — an
    unconfigured pod must never accept the endpoint that flips an automation to
    runnable (spec §8.2). Read per call, not at import, so the setting is the
    live one. Both sides are hashed before the constant-time compare so the
    comparison does not even leak the token's length. Rejections are logged at
    most once a minute per kind (startup also warns about an unset token).
    """
    expected = config.CI_WEBHOOK_TOKEN
    if not expected:
        suppressed = _unconfigured_log.allow()
        if suppressed is not None:
            logger.error(
                "ci-webhook rejected: CI_WEBHOOK_TOKEN is not configured "
                "(%d similar rejections suppressed)",
                suppressed,
            )
        return False
    if x_gitlab_token is None:
        return False
    if not hmac.compare_digest(
        hashlib.sha256(x_gitlab_token.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    ):
        # A rotation done on one side only shows up here, not as silence.
        suppressed = _mismatch_log.allow()
        if suppressed is not None:
            logger.warning(
                "ci-webhook rejected: X-Gitlab-Token does not match "
                "(%d similar rejections suppressed)",
                suppressed,
            )
        return False
    return True


async def verify_ci_webhook_token(
    x_gitlab_token: str | None = Header(default=None),
) -> None:
    """Machine-tier gate: the CI pipeline's shared secret in `X-Gitlab-Token`.

    `src/api/ci_webhook_guard.py` applies the same check as ASGI middleware
    *before the body is read* (a dependency runs only after FastAPI has
    buffered and JSON-decoded it); this dependency keeps the tier's gate on the
    router itself, so the route stays protected if the middleware is ever
    unmounted.
    """
    if not ci_webhook_token_valid(x_gitlab_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing CI webhook token",
        )


async def verify_service_token(authorization: str | None = Header(default=None)):
    """Internal-tier gate. Real service-token verification lands in D17/D20."""
    return authorization
