"""Delete cascade + resume (D18, spec §5.4). Deboard fan-out lives here too.

Ordering principle: stop new work first, reclaim last, audit never deleted.
Five steps, in this exact order; `automations.delete_cascade_step` records the
**last completed** one (null = not begun):

    1  DB: matching_state=deleting, index_generation++, delete revision;
       publish `reload` after commit.
    2  CAPP: 2a delete the Capp -> 2b delete the Keep-owned run-auth Secret ->
       2c clear the encrypted DB key in the SAME transaction as checkpoint 2.
    3  Registry: delete every image under this automation's own path.
    4  Git: archive-mark the script path (bytes and history kept).
    5  DB: matching_state=deleted.

Capp before Secret before registry: never leave a live service without its key
mount, or pointing at a deleted image. The team Secret named by `secret_name`
is never deleted; rows, revisions, runs and script bytes persist forever.

**Correctness lives in the DB checkpoint, not in this process.** Every external
operation is delete-if-exists, so re-running one after a lost response or a lost
checkpoint is harmless. Checkpoints advance with compare-and-set updates
(`WHERE delete_cascade_step = <expected>`), so two runners on the same row — two
API replicas, a repeated DELETE racing a resume — may repeat a step but can
never move the checkpoint backward or skip one. No Python lock is involved.

**Blocking I/O shape (ADR-008).** Every function here is synchronous and runs in
the threadpool. External calls happen with no DB session open: a snapshot is
read in one short session, the external call runs, and the checkpoint is written
in a second short session. The adapters own their network timeouts.

**Failure.** Any external or checkpoint failure stops the attempt at the last
saved step; the automation stays `deleting`, never falsely `deleted`. The error
is reported as a stable, sanitized code — exception messages are never logged or
returned, since CAPP/registry errors can embed URLs or credentials. A repeated
DELETE, `resume_delete` (D20's internal route), or E21's schedule continues it.
"""
import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, update

from src.bl.automations_bl import scoped_get
from src.bl.cascade_adapters import (
    CappDeletionClient,
    RegistryClient,
    capp_name_for,
    run_auth_secret_name,
)
from src.bl.git_client import GitClient
from src.core.db import get_session
from src.core.reload import ReloadPublisher
from src.exceptions import (
    AutomationBuildingError,
    AutomationLifecycleConflictError,
    AutomationNotFoundError,
)
from src.models.db.automation import (
    DELETION_STATES,
    Automation,
    BuildState,
    MatchingState,
)
from src.models.db.automation_revision import AutomationRevision, RevisionAction

logger = logging.getLogger(__name__)

STEP_MARKED_DELETING = 1
STEP_CAPP_RESOURCES_DELETED = 2
STEP_IMAGES_DELETED = 3
STEP_SCRIPT_ARCHIVED = 4
STEP_DELETED = 5

# The encrypted copy of the per-automation `/run` API key. The column is created
# by D16/F24 (migration in keep-migrations, C6) and does not exist yet; step 2c
# clears it in the checkpoint-2 transaction as soon as the ORM model carries it.
# Resolved against the table at call time so no code change is needed then.
RUN_AUTH_KEY_COLUMN = "run_api_key_encrypted"

# A runner makes at most one pass per step plus a few lost compare-and-set
# races; anything beyond this is a bug, not contention.
_MAX_ITERATIONS = 4 * STEP_DELETED


@dataclass(frozen=True)
class CascadeDeps:
    capp: CappDeletionClient
    registry: RegistryClient
    git: GitClient
    publisher: ReloadPublisher


@dataclass(frozen=True)
class CascadeResult:
    """Progress after one cascade attempt.

    `error` is a sanitized operation code (e.g. `capp_delete_failed`) when the
    attempt stopped early, else None. It is safe to log and to return from an
    internal endpoint; it never carries exception text.
    """

    automation_id: UUID
    matching_state: MatchingState
    delete_cascade_step: int | None
    index_generation: int
    error: str | None = None

    @property
    def completed(self) -> bool:
        return self.matching_state == MatchingState.DELETED


class CascadeStepError(Exception):
    def __init__(self, step: int, code: str, cause: BaseException | None = None):
        self.step = step
        self.code = code
        self.cause_type = type(cause).__name__ if cause is not None else None
        super().__init__(code)


# --- admission ------------------------------------------------------------


def begin_delete(
    tenant_id: str, automation_id: UUID, actor: str, publisher: ReloadPublisher
) -> Automation:
    """DELETE admission + cascade step 1, under the row lock shared with edits.

    Idempotent path first: an automation already `deleting`/`deleted` is
    returned unchanged — no new revision, no generation bump, no actor change
    (the original deleter stays on the row through every retry). Otherwise a
    row mid-build is refused with 409 before any state change or external call:
    D18 never cancels a build, and a build that finishes after the cascade would
    recreate an untracked Capp.
    """
    with get_session() as session:
        automation = scoped_get(session, tenant_id, automation_id, for_update=True)
        if automation.matching_state in DELETION_STATES:
            session.expunge(automation)
            return automation
        if automation.build_state == BuildState.BUILDING:
            raise AutomationBuildingError()

        automation.matching_state = MatchingState.DELETING
        automation.delete_cascade_step = STEP_MARKED_DELETING
        automation.index_generation = Automation.index_generation + 1
        automation.updated_by = actor
        automation.updated_at = func.now()
        session.add(automation)
        session.add(
            AutomationRevision(
                automation_id=automation.id,
                action=RevisionAction.DELETE,
                resulting_digest=automation.active_digest,
                actor=actor,
            )
        )
        session.commit()
        session.refresh(automation)
        session.expunge(automation)

    publisher.publish_reload()
    return automation


# --- runner ---------------------------------------------------------------


def _snapshot(automation_id: UUID) -> Automation:
    """Unscoped read by id — the runner acts on ids already admitted per tenant."""
    with get_session() as session:
        automation = session.scalars(
            select(Automation).where(Automation.id == automation_id)
        ).first()
        if automation is None:
            raise AutomationNotFoundError()
        session.expunge(automation)
    return automation


def _result(automation: Automation, error: str | None = None) -> CascadeResult:
    return CascadeResult(
        automation_id=automation.id,
        matching_state=automation.matching_state,
        delete_cascade_step=automation.delete_cascade_step,
        index_generation=automation.index_generation,
        error=error,
    )


def _run_auth_key_clear_values() -> dict:
    if RUN_AUTH_KEY_COLUMN in Automation.__table__.c:
        return {RUN_AUTH_KEY_COLUMN: None}
    return {}


def _checkpoint(automation_id: UUID, completed: int, **values) -> bool:
    """Compare-and-set `delete_cascade_step` from `completed - 1` to `completed`.

    Returns False when another runner already moved the row (or it left
    `deleting`); the caller re-reads and continues from whatever is there.
    """
    expected = completed - 1
    step_predicate = (
        Automation.delete_cascade_step.is_(None)
        if expected == 0
        else Automation.delete_cascade_step == expected
    )
    with get_session() as session:
        result = session.execute(
            update(Automation)
            .where(
                Automation.id == automation_id,
                Automation.matching_state == MatchingState.DELETING,
                step_predicate,
            )
            .values(delete_cascade_step=completed, updated_at=func.now(), **values)
            .execution_options(synchronize_session=False)
        )
        session.commit()
        return result.rowcount == 1


def _external(step: int, code: str, call, *args) -> None:
    try:
        call(*args)
    except Exception as exc:  # noqa: BLE001 — any failure stops the cascade
        raise CascadeStepError(step, code, exc) from exc


def _save(automation_id: UUID, completed: int, **values) -> None:
    try:
        _checkpoint(automation_id, completed, **values)
    except Exception as exc:  # noqa: BLE001
        raise CascadeStepError(completed, "checkpoint_failed", exc) from exc


def _step_mark_deleting(automation: Automation, deps: CascadeDeps) -> None:
    # Only reachable for a `deleting` row with no checkpoint (a row flipped
    # outside `begin_delete`). The state and revision are already there; record
    # the checkpoint and re-signal.
    _save(automation.id, STEP_MARKED_DELETING)
    deps.publisher.publish_reload()


def _step_delete_capp_resources(automation: Automation, deps: CascadeDeps) -> None:
    wallet = automation.namespace
    # 2a then 2b, always both — even with no capp_deployment_id: a first deploy
    # can create the run-auth Secret and fail before the id is stored.
    _external(
        STEP_CAPP_RESOURCES_DELETED,
        "capp_delete_failed",
        deps.capp.delete_capp,
        wallet,
        capp_name_for(automation.id, automation.capp_deployment_id),
    )
    _external(
        STEP_CAPP_RESOURCES_DELETED,
        "run_auth_secret_delete_failed",
        deps.capp.delete_secret,
        wallet,
        run_auth_secret_name(automation.id),
    )
    # 2c: the DB key goes in the checkpoint's own transaction, and only after
    # both CAPP deletions succeeded — a failed 2a/2b keeps it for the retry.
    _save(automation.id, STEP_CAPP_RESOURCES_DELETED, **_run_auth_key_clear_values())


def _step_delete_images(automation: Automation, deps: CascadeDeps) -> None:
    step = STEP_IMAGES_DELETED
    try:
        digests = list(deps.registry.list_image_digests(automation.id))
    except Exception as exc:  # noqa: BLE001
        raise CascadeStepError(step, "registry_list_failed", exc) from exc
    for digest in digests:
        _external(
            step,
            "registry_delete_failed",
            deps.registry.delete_image,
            automation.id,
            digest,
        )
    # Advance only on an empty inventory: a digest pushed while we deleted (a
    # late build, C2/C3) must not be left behind under a "done" checkpoint.
    try:
        remaining = list(deps.registry.list_image_digests(automation.id))
    except Exception as exc:  # noqa: BLE001
        raise CascadeStepError(step, "registry_list_failed", exc) from exc
    if remaining:
        raise CascadeStepError(step, "registry_inventory_not_empty")
    _save(automation.id, step)


def _step_archive_script(automation: Automation, deps: CascadeDeps) -> None:
    # `updated_by` is the deleting actor: set at step 1 and never touched again
    # (edits and toggles are refused once deleting).
    _external(
        STEP_SCRIPT_ARCHIVED,
        "git_archive_failed",
        deps.git.archive,
        automation.id,
        automation.updated_by,
    )
    _save(automation.id, STEP_SCRIPT_ARCHIVED)


def _step_mark_deleted(automation: Automation, deps: CascadeDeps) -> None:
    _save(automation.id, STEP_DELETED, matching_state=MatchingState.DELETED)


_STEPS = {
    STEP_MARKED_DELETING: _step_mark_deleting,
    STEP_CAPP_RESOURCES_DELETED: _step_delete_capp_resources,
    STEP_IMAGES_DELETED: _step_delete_images,
    STEP_SCRIPT_ARCHIVED: _step_archive_script,
    STEP_DELETED: _step_mark_deleted,
}


def run_cascade(automation_id: UUID, deps: CascadeDeps) -> CascadeResult:
    """Continue the cascade from the step after `delete_cascade_step`.

    - `deleted` → no-op, returns final progress.
    - `active` / `inactive` → AutomationLifecycleConflictError: resuming is never
      an implicit delete.
    - `deleting` → runs the remaining steps; stops at the first failure and
      returns progress with a sanitized `error` code.
    """
    automation = _snapshot(automation_id)
    for _ in range(_MAX_ITERATIONS):
        if automation.matching_state == MatchingState.DELETED:
            return _result(automation)
        if automation.matching_state != MatchingState.DELETING:
            raise AutomationLifecycleConflictError(automation.matching_state.value)

        next_step = (automation.delete_cascade_step or 0) + 1
        try:
            _STEPS[next_step](automation, deps)
        except CascadeStepError as exc:
            logger.warning(
                "automations: delete cascade for %s stopped at step %s: %s (%s)",
                automation.id,
                exc.step,
                exc.code,
                exc.cause_type,
            )
            return _result(automation, error=exc.code)
        # Re-read whether or not our checkpoint won: a lost compare-and-set
        # means another runner advanced the row, and we continue from there.
        automation = _snapshot(automation_id)

    logger.error(
        "automations: delete cascade for %s made no progress in %s iterations",
        automation_id,
        _MAX_ITERATIONS,
    )
    return _result(automation, error="cascade_no_progress")


def resume_delete(automation_id: UUID, deps: CascadeDeps) -> CascadeResult:
    """Business logic behind D20's `POST /internal/automations/{id}/resume-delete`.

    Internal and unscoped by tenant (the reconciler acts across tenants). Same
    runner as a repeated user DELETE; every step is safe to repeat.
    """
    return run_cascade(automation_id, deps)


def run_cascade_in_background(automation_id: UUID, deps: CascadeDeps) -> None:
    """One bounded background attempt started by DELETE. Never raises.

    Opens its own sessions and loads by id — it never captures the request's
    session. If this attempt dies with the process, the checkpoint is intact and
    a repeated DELETE or `resume_delete` continues it.
    """
    try:
        run_cascade(automation_id, deps)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "automations: delete cascade attempt for %s crashed (%s)",
            automation_id,
            type(exc).__name__,
        )


# --- deboard --------------------------------------------------------------


@dataclass(frozen=True)
class DeboardSummary:
    """Outcome of one `deboard_wallet` pass, derived from the child rows.

    - `deleted`: the child reached `deleted` (including ones already deleted
      by an earlier pass that this pass finished).
    - `in_progress`: deletion was admitted (`deleting`) but this attempt stopped
      at a checkpoint — a later call, repeated DELETE or resume continues it.
    - `skipped_building`: refused by the build guard; untouched, never
      cancelled. A later call deletes it once its build settles.
    - `failed`: deletion could not even be admitted (unexpected error); the
      child is still active/inactive.
    """

    deleted: tuple[UUID, ...] = ()
    in_progress: tuple[UUID, ...] = ()
    skipped_building: tuple[UUID, ...] = ()
    failed: tuple[UUID, ...] = ()

    def counts(self) -> dict[str, int]:
        return {
            "deleted": len(self.deleted),
            "in_progress": len(self.in_progress),
            "skipped_building": len(self.skipped_building),
            "failed": len(self.failed),
        }


def deboard_wallet(
    tenant_id: str, wallet_name: str, actor: str, deps: CascadeDeps
) -> DeboardSummary:
    """Run the single-automation delete for every automation on a tenant's wallet.

    Spec §5.4: deboard is the same cascade fanned out — no separate cascade, no
    deboard table; progress is read back from the child rows, so calling again
    simply resumes. Scoped by tenant AND wallet (the `(tenant_id, namespace)`
    index): two tenants can target the same wallet name, and deboarding one must
    never touch the other's automations. Unbounded on purpose — the authoring
    list's row limit must not silently leave automations behind.

    One child's failure never stops the rest. Never touches the wallet itself
    (namespace, quota, permissions) or any team Secret. No HTTP route: whoever
    adds a caller (F25 / ops) owns its entry point and authorization.
    """
    with get_session() as session:
        automation_ids = list(
            session.scalars(
                select(Automation.id)
                .where(
                    Automation.tenant_id == tenant_id,
                    Automation.namespace == wallet_name,
                    Automation.matching_state != MatchingState.DELETED,
                )
                .order_by(Automation.created_at, Automation.id)
            ).all()
        )

    deleted: list[UUID] = []
    in_progress: list[UUID] = []
    skipped_building: list[UUID] = []
    failed: list[UUID] = []
    for automation_id in automation_ids:
        try:
            begin_delete(tenant_id, automation_id, actor, deps.publisher)
        except AutomationBuildingError:
            skipped_building.append(automation_id)
            continue
        except Exception as exc:  # noqa: BLE001 — isolate children
            logger.warning(
                "automations: deboard of %s could not admit %s (%s)",
                wallet_name,
                automation_id,
                type(exc).__name__,
            )
            failed.append(automation_id)
            continue

        try:
            result = run_cascade(automation_id, deps)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "automations: deboard cascade for %s crashed (%s)",
                automation_id,
                type(exc).__name__,
            )
            in_progress.append(automation_id)
            continue
        (deleted if result.completed else in_progress).append(automation_id)

    summary = DeboardSummary(
        deleted=tuple(deleted),
        in_progress=tuple(in_progress),
        skipped_building=tuple(skipped_building),
        failed=tuple(failed),
    )
    logger.info(
        "automations: deboard pass for tenant %s wallet %s: %s",
        tenant_id,
        wallet_name,
        summary.counts(),
    )
    return summary
