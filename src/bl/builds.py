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
deliveries of one webhook cut over once (both CAPP calls are idempotent). A
`failed` for the same commit that lands during the CAPP call does not strand
CAPP ahead of the row: the re-check finds `build_failed` for this commit and
the success still cuts over (`_failure_raced`) — CAPP already serves it.

**Failure.** `build_state=build_failed`, lock released, `active_digest` and the
deployment untouched: a first build has nothing to serve; an edit keeps serving
the previous digest (spec §5.1). PUT is allowed again, so the author retries
with a fresh edit. A failure report is bound only by `building_sha`, not by an
image: any holder of the shared webhook token who knows an automation's id and
building SHA can fail its build. The worst case is a forced re-edit (no image
reaches CAPP this way, and a success already mid-cutover still wins); closing it
needs per-pipeline tokens (open question).

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
from sqlalchemy.exc import SQLAlchemyError

from src import config
from src.bl.capp_client import CappClient, CappDeployment
from src.bl.cascade_adapters import (
    CappDeletionClient,
    get_default_capp_deletion_client,
    run_auth_secret_name,
)
from src.contracts.validation_errors import ErrorCode, FieldError
from src.core.automation_events import AutomationEventPublisher
from src.core.db import get_session, lock_first
from src.core.reload import ReloadPublisher
from src.exceptions import (
    AutomationBusyError,
    AutomationNotFoundError,
    AutomationValidationError,
    BuildNotRecordedError,
    CappUnavailableError,
)
from src.models.api.ci_webhook import (
    BuildOutcome,
    CiWebhookIn,
    CiWebhookOut,
    CiWebhookResult,
    IgnoredReason,
    is_valid_repository,
)
from src.models.db.automation import DELETION_STATES, Automation, BuildState
from src.models.db.automation_revision import AutomationRevision, RevisionAction

logger = logging.getLogger(__name__)

BUILD_EVENT_TYPE = "build"
# Recorded as `actor` on build-outcome revisions: the machine tier has no human
# identity, and the shared token does not say which pipeline sent the webhook.
BUILD_ACTOR = "ci-webhook"
# The human actions that commit a script and so request a build.
_REQUESTING_ACTIONS = (RevisionAction.CREATE, RevisionAction.EDIT)


@dataclass(frozen=True)
class _BuildTarget:
    """What the CAPP call needs, read before the connection is released."""

    tenant_id: str
    wallet: str
    secret_name: str | None
    capp_deployment_id: str | None


def _admission(automation: Automation, commit_sha: str) -> IgnoredReason | None:
    """Why this webhook must be ignored, or None when it may apply."""
    if automation.matching_state in DELETION_STATES:
        return IgnoredReason.DELETING
    if automation.build_state != BuildState.BUILDING:
        return IgnoredReason.NOT_BUILDING
    if automation.building_sha != commit_sha:
        return IgnoredReason.SHA_MISMATCH
    return None


def _load(session, automation_id: UUID, lock: bool = False) -> Automation:
    query = select(Automation).where(Automation.id == automation_id)
    # The lock is the one edit/toggle/DELETE admission takes; its wait is
    # bounded (`lock_first`) so a busy row is a retryable 503, never a 500.
    automation = lock_first(session, query) if lock else session.scalars(query).first()
    if automation is None:
        raise AutomationNotFoundError()
    return automation


def _failure_raced(session, automation: Automation, commit_sha: str) -> bool:
    """True when the row is `build_failed` *for this commit* — a failure
    recorded while this success was inside its CAPP call.

    `building_sha` is cleared by the failure, so the commit is recovered from
    the latest create/edit revision. Any newer edit has claimed the row
    (`building`, not `build_failed`) or written a newer revision, so it is
    never overridden. Only reached after CAPP accepted this commit's image.
    """
    if automation.build_state != BuildState.BUILD_FAILED:
        return False
    latest_sha = session.scalars(
        select(AutomationRevision.git_sha)
        .where(
            AutomationRevision.automation_id == automation.id,
            AutomationRevision.action.in_(_REQUESTING_ACTIONS),
        )
        .order_by(AutomationRevision.created_at.desc())
        .limit(1)
    ).first()
    return latest_sha == commit_sha


def _build_revision(
    event: CiWebhookIn, action: RevisionAction, digest: str | None
) -> AutomationRevision:
    """The audit row for an applied build outcome (spec §8.4: no unaudited
    state transition). Written in the same transaction as the state change."""
    return AutomationRevision(
        automation_id=event.automation_id,
        action=action,
        git_sha=event.commit_sha,
        resulting_digest=digest,
        actor=BUILD_ACTOR,
    )


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


def configured_image_repository() -> str | None:
    """`AUTOMATION_IMAGE_REPOSITORY` without a trailing slash, or None when it
    is unset or could never match an image (fail closed — see
    `is_valid_repository`; docker references are lowercase)."""
    prefix = config.AUTOMATION_IMAGE_REPOSITORY.rstrip("/")
    if not prefix or not is_valid_repository(prefix):
        return None
    return prefix


def _require_own_image(event: CiWebhookIn) -> None:
    """The image must come from this automation's own repository.

    The webhook token is one secret shared by every pipeline, and commit SHAs
    are readable by anyone with repo access — so without this, any token holder
    could cut a building automation over to an arbitrary image (or another
    automation's) in that automation's wallet, with its secrets mounted.
    """
    prefix = configured_image_repository()
    if prefix is None:
        logger.error(
            "ci-webhook refused: AUTOMATION_IMAGE_REPOSITORY is unset or not a "
            "valid lowercase docker repository (automation=%s)",
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
    deletion: CappDeletionClient | None = None,
) -> CiWebhookOut:
    if deletion is None:
        deletion = get_default_capp_deletion_client()
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
    secret_refs = [target.secret_name] if target.secret_name else []
    try:
        if target.capp_deployment_id is None:
            created = capp.create_deployment(
                automation_id=event.automation_id,
                wallet=target.wallet,
                image=event.image,
                secret_refs=secret_refs,
            )
        else:
            # No wallet: it is fixed at create (authoring refuses a change once
            # deployed). The secret can change with an edit, so it travels.
            capp.rollout(
                deployment_id=target.capp_deployment_id,
                image=event.image,
                secret_refs=secret_refs,
            )
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

    # Phase 3 — atomic cutover, admission re-checked under the row lock. CAPP
    # has accepted the image by now, so a failure to record it leaves CAPP
    # ahead of the row: logged, and answered 503 so CI redelivers.
    ignored: IgnoredReason | None = None
    try:
        with get_session() as session:
            automation = _load(session, event.automation_id, lock=True)
            reason = _admission(automation, event.commit_sha)
            if reason == IgnoredReason.NOT_BUILDING and _failure_raced(
                session, automation, event.commit_sha
            ):
                # A `failed` for this same commit landed while CAPP was
                # accepting the image. CAPP serves it now; the row follows.
                logger.warning(
                    "ci-webhook: failure for automation=%s sha=%s was recorded "
                    "during the CAPP %s; success overrides it",
                    event.automation_id,
                    event.commit_sha,
                    operation,
                )
                reason = None
            if reason is not None:
                if automation.active_digest != event.image:
                    # Not a duplicate delivery: CAPP accepted the image but the
                    # row moved on mid-call (deletion started, or the lock was
                    # released). CAPP is now ahead of the row — a rollout
                    # serves `event.image`; a first-build create left a
                    # deployment whose id was never stored.
                    logger.warning(
                        "ci-webhook CAPP ahead of row: %s accepted image=%s but "
                        "admission failed at commit (reason=%s automation=%s "
                        "sha=%s)",
                        operation,
                        event.image,
                        reason.value,
                        event.automation_id,
                        event.commit_sha,
                    )
                # Otherwise a concurrent delivery of this same webhook cut over.
                ignored = reason
            else:
                build_event = _cut_over(session, automation, event, created)
    except (AutomationBusyError, SQLAlchemyError) as exc:
        logger.warning(
            "ci-webhook CAPP ahead of row: %s accepted image=%s but the cutover "
            "could not be recorded (%s): automation=%s sha=%s; CI must redeliver",
            operation,
            event.image,
            type(exc).__name__,
            event.automation_id,
            event.commit_sha,
        )
        if isinstance(exc, AutomationBusyError):
            raise
        raise BuildNotRecordedError() from exc

    if ignored is not None:
        if ignored == IgnoredReason.DELETING and created is not None:
            # The cascade may already have run its CAPP step (a 404 then —
            # nothing existed yet), and never runs it again: undo our create.
            _tear_down(event, target.wallet, created, deletion)
        return _ignored(event, ignored)

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


def _cut_over(
    session, automation: Automation, event: CiWebhookIn, created: CappDeployment | None
) -> dict:
    """Write the cutover + its audit row and commit; the build event to publish."""
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
            AutomationRevision.action.in_(_REQUESTING_ACTIONS),
        )
        .values(resulting_digest=event.image)
    )
    session.add(_build_revision(event, RevisionAction.BUILD_SUCCEEDED, event.image))
    session.commit()
    session.refresh(automation)
    return _build_event(automation, event.commit_sha)


def _tear_down(
    event: CiWebhookIn,
    wallet: str,
    created: CappDeployment,
    deletion: CappDeletionClient,
) -> None:
    """Best-effort removal of a deployment created for an automation that
    started deleting mid-call — the same two deletions as the cascade's CAPP
    step (Capp, then the Keep-owned run-auth Secret), in the wallet we created
    it in. A failure leaves it orphaned for the cascade/E21 to sweep."""
    try:
        deletion.delete_capp(wallet, created.deployment_id)
        deletion.delete_secret(wallet, run_auth_secret_name(event.automation_id))
    except Exception as exc:  # noqa: BLE001 — best-effort, the answer stays `ignored`
        logger.warning(
            "ci-webhook: could not tear down the deployment created for "
            "deleting automation=%s in wallet %s (%s); it is orphaned until "
            "swept by automation_id",
            event.automation_id,
            wallet,
            type(exc).__name__,
        )
        return
    logger.info(
        "ci-webhook: tore down the deployment created for deleting automation=%s",
        event.automation_id,
    )


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
        session.add(_build_revision(event, RevisionAction.BUILD_FAILED, None))
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
