"""Deletion-aware run finalization + build-mutation fencing (D18, spec §5.4, §9.3).

These are the seams D15 (CI webhook cutover), D17 (submit loop) and D20/E21
(reconciler repair) call so deletion is never misreported. None of those
stories has landed; the functions are complete and tested on their own.

**Runs killed by deletion.** Deleting the Capp tears down any in-flight `/run`
(no drain grace). The disrupted call must be recorded as `failed` +
`failure_class=terminated_by_deletion` and must NOT page the team or go to the
script-failure dead-letter — "never page a team as a script failure for an
explicit delete" (§9.3). `finalize_terminated_by_deletion` is that single path;
it performs no notification by construction, so a caller routing a disrupted
call through it cannot notify by accident.

**Terminal writes are compare-and-set.** A run leaves `pending`/`submitted`
exactly once: `complete_run_if_open` updates only while the row is still open.
So a success returned during teardown and a deletion termination race safely —
whichever commits first stands, the late one is a no-op. CAPP's `204` never
bulk-fails `submitted` rows: an existing call may still legitimately succeed.
"""
from uuid import UUID

from sqlalchemy import func, select, update

from src.core.db import get_session, lock_first
from src.exceptions import (
    AutomationLifecycleConflictError,
    AutomationNotFoundError,
    RunNotFoundError,
)
from src.models.db.automation import DELETION_STATES, Automation, MatchingState
from src.models.db.automation_run import (
    AutomationRun,
    FailureClass,
    RunState,
    SuppressionReason,
)

OPEN_RUN_STATES = (RunState.PENDING, RunState.SUBMITTED)


def _matching_state(automation_id: UUID) -> MatchingState:
    with get_session() as session:
        state = session.scalar(
            select(Automation.matching_state).where(Automation.id == automation_id)
        )
    if state is None:
        raise AutomationNotFoundError()
    return state


def deletion_started(automation_id: UUID) -> bool:
    """True once the automation is `deleting` or `deleted` (one-way).

    D17 checks this before the first `/run` and before every retry; E21/D20
    checks it before any re-drive (C1) — a deleting automation is never invoked.
    """
    return _matching_state(automation_id) in DELETION_STATES


def complete_run_if_open(run_id: UUID, **values) -> bool:
    """Write a terminal outcome only if the run is still pending/submitted.

    Returns False when a terminal outcome was already committed — the caller
    must then read the row rather than report its own result.
    """
    with get_session() as session:
        result = session.execute(
            update(AutomationRun)
            .where(
                AutomationRun.run_id == run_id,
                AutomationRun.state.in_(OPEN_RUN_STATES),
            )
            .values(finished_at=func.now(), **values)
            .execution_options(synchronize_session=False)
        )
        session.commit()
        return result.rowcount == 1


def _load_run(run_id: UUID) -> AutomationRun:
    with get_session() as session:
        run = session.get(AutomationRun, run_id)
        if run is None:
            raise RunNotFoundError()
        session.expunge(run)
    return run


def finalize_terminated_by_deletion(run_id: UUID) -> AutomationRun:
    """Attribute an open run of a deleting/deleted automation to the deletion.

    - `submitted` (a `/run` was, or may have been, in flight) → `failed` +
      `terminated_by_deletion` + `finished_at`. `attempts`, `automation_digest`
      and any recorded `outcome_status` are kept; no wrapper outcome is invented.
    - `pending` (never invoked) → `suppressed` + `suppression_reason=inactive`:
      the automation is no longer active, the same skip D17 audits at its
      re-check (contracts §Submit). It was never executed, so it is not a
      deletion-killed run.
    - already terminal → returned unchanged; a committed success or failure is
      never overwritten.
    - automation still active/inactive → AutomationLifecycleConflictError: an
      ordinary failure must keep its ordinary attribution and notification.

    Works regardless of cascade progress, including after step 5.
    """
    run = _load_run(run_id)
    if run.state not in OPEN_RUN_STATES:
        return run
    state = _matching_state(run.automation_id)
    if state not in DELETION_STATES:
        raise AutomationLifecycleConflictError(state.value)

    if run.state == RunState.SUBMITTED:
        complete_run_if_open(
            run_id,
            state=RunState.FAILED,
            failure_class=FailureClass.TERMINATED_BY_DELETION,
        )
    else:
        complete_run_if_open(
            run_id,
            state=RunState.SUPPRESSED,
            suppression_reason=SuppressionReason.INACTIVE,
        )
    return _load_run(run_id)


def lock_for_build_mutation(session, automation_id: UUID) -> Automation | None:
    """Row-lock an automation for a build cutover / CAPP create-or-rollout (D15/D16).

    Returns None when the automation is `deleting`/`deleted`: a late CI webhook
    or rollout must then be a no-op, or it would recreate a Capp and run-auth
    Secret nobody tracks. Uses the same row lock as DELETE admission, so either
    the build is admitted first (DELETE then sees `building` → 409) or the delete
    is (the build sees `deleting` → no-op). The caller keeps `session` open for
    its state write and commits it; the lock is held until then — so the caller
    must not do CAPP or git I/O inside that session (claim, commit, call, then
    finish in a second short transaction, as `update_automation` does). A row
    locked past DB_LOCK_TIMEOUT_MS raises AutomationBusyError.
    """
    automation = lock_first(
        session, select(Automation).where(Automation.id == automation_id)
    )
    if automation is None:
        raise AutomationNotFoundError()
    if automation.matching_state in DELETION_STATES:
        return None
    return automation
