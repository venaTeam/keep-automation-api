"""Build completion — the CI webhook's cutover logic (D15, spec §5.1, §8.2).

Synchronous like the rest of the BL; the route runs it via `run_in_threadpool`.

**Admission.** A webhook acts only on the build it was started for: the row must
be `build_state=building`, its `building_sha` must equal the payload's
`commit_sha`, and it must not be deleting. Anything else — a replay of an
applied build, a late webhook for a SHA a newer edit superseded, a build that
finished after the reconciler released its lock — is answered `ignored` and
changes nothing, CAPP included. That is the whole of "idempotent per
(automation, SHA)": the first delivery clears `building_sha`, so every later
one fails admission.

**Success.** CAPP first, then the row. The deployment is created (first build)
or rolled out (later builds) with **no DB connection held** — a hung CAPP call
must not pin the small shared pool (ADR-008; same shape as authoring's git
call). Only once CAPP has accepted the image does a short transaction advance
`active_digest`, so the digest we record is always the one being served. A
CAPP failure raises `CappUnavailableError` (→ 503) with the row untouched: still
`building`, lock held, old digest live (spec §6.3) — a replay or the reconciler
retries. The commit re-checks admission under `FOR UPDATE`, so two racing
deliveries of one webhook cut over once (both CAPP calls are idempotent).

**Failure.** `build_state=build_failed`, lock released, `active_digest` and the
deployment untouched: a first build has nothing to serve; an edit keeps serving
the previous digest (spec §5.1). PUT is allowed again, so the author retries
with a fresh edit.

The matching axis is never touched here: a first build lands `inactive`
("ready to enable", D18 enables); an edit of an active automation simply
starts serving the new digest.

**Not tenant-scoped, deliberately.** The machine tier has no tenant — CI knows
only the automation id from the script path — so the row is found by id alone.
The tenant comes *from* the row, and is stamped on the SSE event so D19 routes
it to that tenant only. Nothing about the row is returned to the caller.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update

from src import config
from src.bl.capp_client import CappClient, CappDeployment
from src.contracts.validation_errors import ErrorCode, FieldError
from src.core.automation_events import AutomationEventPublisher
from src.core.db import get_session
from src.core.reload import ReloadPublisher
from src.exceptions import (
    AutomationNotFoundError,
    AutomationValidationError,
    CappUnavailableError,
)
from src.models.api.ci_webhook import (
    BuildOutcome,
    CiWebhookIn,
    CiWebhookOut,
    CiWebhookResult,
    IgnoredReason,
)
from src.models.db.automation import Automation, BuildState, MatchingState
from src.models.db.automation_revision import AutomationRevision

logger = logging.getLogger(__name__)

BUILD_EVENT_TYPE = "build"
_DELETION_STATES = (MatchingState.DELETING, MatchingState.DELETED)


@dataclass(frozen=True)
class _BuildTarget:
    """What the CAPP call needs, read before the connection is released."""

    tenant_id: str
    wallet: str
    secret_name: str | None
    capp_deployment_id: str | None


def _admission(automation: Automation, commit_sha: str) -> IgnoredReason | None:
    """Why this webhook must be ignored, or None when it may apply."""
    if automation.matching_state in _DELETION_STATES:
        return IgnoredReason.DELETING
    if automation.build_state != BuildState.BUILDING:
        return IgnoredReason.NOT_BUILDING
    if automation.building_sha != commit_sha:
        return IgnoredReason.SHA_MISMATCH
    return None


def _load(session, automation_id: UUID, lock: bool = False) -> Automation:
    query = select(Automation).where(Automation.id == automation_id)
    if lock:
        query = query.with_for_update()
    automation = session.scalars(query).first()
    if automation is None:
        raise AutomationNotFoundError()
    return automation


def _ignored(event: CiWebhookIn, reason: IgnoredReason) -> CiWebhookOut:
    logger.info(
        "ci-webhook ignored: automation=%s sha=%s reason=%s",
        event.automation_id,
        event.commit_sha,
        reason.value,
    )
    return CiWebhookOut(status=CiWebhookResult.IGNORED, reason=reason)


def _build_event(automation: Automation, commit_sha: str) -> dict:
    return {
        "type": BUILD_EVENT_TYPE,
        "automation_id": str(automation.id),
        "tenant_id": automation.tenant_id,
        "commit_sha": commit_sha,
        "build_state": automation.build_state.value,
        "matching_state": automation.matching_state.value,
        "active_digest": automation.active_digest,
    }


def _require_own_image(event: CiWebhookIn) -> None:
    """The image must come from this automation's own repository.

    The webhook token is one secret shared by every pipeline, and commit SHAs
    are readable by anyone with repo access — so without this, any token holder
    could cut a building automation over to an arbitrary image (or another
    automation's) in that automation's wallet, with its secrets mounted.
    """
    prefix = config.AUTOMATION_IMAGE_REPOSITORY.rstrip("/")
    if not prefix:
        logger.error(
            "ci-webhook refused: AUTOMATION_IMAGE_REPOSITORY is not configured "
            "(automation=%s)",
            event.automation_id,
        )
        raise CappUnavailableError("image repository not configured")
    repository = event.image.split("@", 1)[0]
    if repository != f"{prefix}/{event.automation_id}":
        logger.warning(
            "ci-webhook refused: image repository %r is not this automation's "
            "(automation=%s sha=%s)",
            repository,
            event.automation_id,
            event.commit_sha,
        )
        raise AutomationValidationError(
            [
                FieldError(
                    field="image",
                    code=ErrorCode.INVALID_VALUE,
                    message="image must be built in this automation's repository",
                )
            ]
        )


def apply_build_result(
    event: CiWebhookIn,
    capp: CappClient,
    reload: ReloadPublisher,
    events: AutomationEventPublisher,
) -> CiWebhookOut:
    # Phase 1 — admission + snapshot, connection released before any CAPP call.
    with get_session() as session:
        automation = _load(session, event.automation_id)
        reason = _admission(automation, event.commit_sha)
        if reason is not None:
            return _ignored(event, reason)
        target = _BuildTarget(
            tenant_id=automation.tenant_id,
            wallet=automation.namespace,
            secret_name=automation.secret_name,
            capp_deployment_id=automation.capp_deployment_id,
        )

    if event.status == BuildOutcome.FAILED:
        return _record_failure(event, events)

    _require_own_image(event)

    # Phase 2 — deploy (first build) or roll out (edit), no connection held.
    # Raises CappUnavailableError → 503 with the row untouched.
    created: CappDeployment | None = None
    operation = "create" if target.capp_deployment_id is None else "rollout"
    try:
        if target.capp_deployment_id is None:
            created = capp.create_deployment(
                automation_id=event.automation_id,
                wallet=target.wallet,
                image=event.image,
                secret_refs=[target.secret_name] if target.secret_name else [],
            )
        else:
            capp.rollout(deployment_id=target.capp_deployment_id, image=event.image)
    except CappUnavailableError as exc:
        # The only server-side trace of a stuck build: the 503 handler logs
        # nothing. Type name only — the text must never reach logs (§3.4).
        logger.warning(
            "ci-webhook CAPP %s failed (%s): automation=%s sha=%s; build stays "
            "locked, old digest serving",
            operation,
            type(exc).__name__,
            event.automation_id,
            event.commit_sha,
        )
        raise

    # Phase 3 — atomic cutover, admission re-checked under the row lock.
    with get_session() as session:
        automation = _load(session, event.automation_id, lock=True)
        reason = _admission(automation, event.commit_sha)
        if reason is not None:
            # A concurrent delivery of this same webhook already cut over.
            return _ignored(event, reason)
        if created is not None:
            automation.capp_deployment_id = created.deployment_id
            automation.deployment_url = created.deployment_url
        automation.active_digest = event.image
        automation.contract_version = event.contract_version
        automation.build_state = BuildState.IDLE
        automation.building_sha = None
        automation.build_lock_deadline = None
        automation.updated_at = datetime.now(timezone.utc)
        session.add(automation)
        # The revision that requested this build learns what it produced.
        session.execute(
            update(AutomationRevision)
            .where(
                AutomationRevision.automation_id == event.automation_id,
                AutomationRevision.git_sha == event.commit_sha,
            )
            .values(resulting_digest=event.image)
        )
        session.commit()
        session.refresh(automation)
        build_event = _build_event(automation, event.commit_sha)

    logger.info(
        "ci-webhook cutover: automation=%s sha=%s image=%s first_deploy=%s",
        event.automation_id,
        event.commit_sha,
        event.image,
        created is not None,
    )
    # After commit, best-effort: neither may undo a committed cutover.
    reload.publish_reload()
    events.publish(build_event)
    return CiWebhookOut(status=CiWebhookResult.APPLIED)


def _record_failure(
    event: CiWebhookIn, events: AutomationEventPublisher
) -> CiWebhookOut:
    with get_session() as session:
        automation = _load(session, event.automation_id, lock=True)
        reason = _admission(automation, event.commit_sha)
        if reason is not None:
            return _ignored(event, reason)
        automation.build_state = BuildState.BUILD_FAILED
        automation.building_sha = None
        automation.build_lock_deadline = None
        automation.updated_at = datetime.now(timezone.utc)
        session.add(automation)
        session.commit()
        session.refresh(automation)
        build_event = _build_event(automation, event.commit_sha)

    logger.info(
        "ci-webhook build failed: automation=%s sha=%s",
        event.automation_id,
        event.commit_sha,
    )
    # No reload: nothing the matcher reads changed.
    events.publish(build_event)
    return CiWebhookOut(status=CiWebhookResult.APPLIED)
