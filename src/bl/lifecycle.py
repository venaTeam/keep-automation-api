"""Matching-axis lifecycle: enable / disable (D18, spec §5.3, §8.1).

Enable and disable change **matching only**. They never call CAPP — the
deployment keeps serving, and runs already in flight finish (D18 decision; the
CAPP request doc's traffic toggle is deliberately not used).

Each transition is one short row-locked transaction: state, `index_generation`
bump, `updated_by`/`updated_at`, one `automation_revisions` row. The `reload`
signal is published only **after** commit — publishing first would let a
subscriber reload before the change is visible and keep the stale definition
until the next periodic reload.

Repeated requests are accepted, not rejected: the state stays as requested,
and generation, audit row and publish all happen again (an explicit request is
always audited, spec §8.4).

Synchronous — routes run these via `run_in_threadpool` (ADR-008).
"""
from uuid import UUID

from sqlalchemy import func

from src.bl.automations_bl import scoped_get
from src.contracts.validation_errors import ErrorCode, FieldError
from src.core.db import get_session
from src.core.reload import ReloadPublisher
from src.exceptions import AutomationLifecycleConflictError, AutomationValidationError
from src.models.db.automation import DELETION_STATES, Automation, MatchingState
from src.models.db.automation_revision import AutomationRevision, RevisionAction


def _toggle(
    tenant_id: str,
    automation_id: UUID,
    actor: str,
    publisher: ReloadPublisher,
    target: MatchingState,
    action: RevisionAction,
) -> Automation:
    with get_session() as session:
        automation = scoped_get(session, tenant_id, automation_id, for_update=True)
        if automation.matching_state in DELETION_STATES:
            raise AutomationLifecycleConflictError(automation.matching_state.value)
        # Building alone does not block a toggle: an active automation keeps
        # serving `active_digest=old` through an edit's build (spec §5.3).
        if target == MatchingState.ACTIVE and not (
            automation.active_digest and automation.active_digest.strip()
        ):
            raise AutomationValidationError(
                [
                    FieldError(
                        field="active_digest",
                        code=ErrorCode.ACTIVE_DIGEST_REQUIRED,
                        message=(
                            "Cannot enable an automation that has never built "
                            "successfully; wait for the first build to finish."
                        ),
                    )
                ]
            )

        automation.matching_state = target
        automation.index_generation = Automation.index_generation + 1
        automation.updated_by = actor
        automation.updated_at = func.now()
        session.add(automation)
        session.add(
            AutomationRevision(
                automation_id=automation.id,
                action=action,
                resulting_digest=automation.active_digest,
                actor=actor,
            )
        )
        session.commit()
        session.refresh(automation)
        session.expunge(automation)

    publisher.publish_reload()
    return automation


def enable_automation(
    tenant_id: str, automation_id: UUID, actor: str, publisher: ReloadPublisher
) -> Automation:
    return _toggle(
        tenant_id,
        automation_id,
        actor,
        publisher,
        MatchingState.ACTIVE,
        RevisionAction.ENABLE,
    )


def disable_automation(
    tenant_id: str, automation_id: UUID, actor: str, publisher: ReloadPublisher
) -> Automation:
    return _toggle(
        tenant_id,
        automation_id,
        actor,
        publisher,
        MatchingState.INACTIVE,
        RevisionAction.DISABLE,
    )
