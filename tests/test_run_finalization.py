"""Deletion-aware run finalization + build fencing (D18 phase 3)."""
import threading
import time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from src.bl import cascade, run_finalization
from src.core.db import get_session
from src.exceptions import (
    AutomationBuildingError,
    AutomationLifecycleConflictError,
    AutomationNotFoundError,
    RunNotFoundError,
)
from src.models.db.automation import BuildState
from src.models.db.automation_run import AutomationRun, RunState
from tests.conftest import TENANT
from tests.fakes import World, deps_for, seed_external
from tests.helpers import DIGEST, VALID, create_built, row


@pytest.fixture()
def world():
    return World()


@pytest.fixture()
def automation_id(client, test_engine):
    return UUID(create_built(client, test_engine))


def _insert_run(automation_id, state=RunState.SUBMITTED, attempts=2) -> UUID:
    run = AutomationRun(
        automation_id=automation_id,
        tenant_id=TENANT,
        history_id=f"h-{uuid4()}",
        fingerprint="fp",
        payload={"severity": "critical"},
        state=state,
        matched_m=1,
        attempts=attempts,
        automation_digest=DIGEST,
    )
    with get_session() as session:
        session.add(run)
        session.commit()
        return run.run_id


def _run_row(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT * FROM automation_runs WHERE run_id = :id"), {"id": str(run_id)}
        ).one()


def _delete(automation_id, world, complete=True):
    deps = deps_for(world)
    cascade.begin_delete(TENANT, automation_id, "deleter@keep", deps.publisher)
    if complete:
        cascade.run_cascade(automation_id, deps)


# --- terminated_by_deletion ----------------------------------------------


@pytest.mark.parametrize("cascade_finished", [False, True], ids=["deleting", "deleted"])
def test_disrupted_submitted_run_is_terminated_by_deletion(
    test_engine, world, automation_id, cascade_finished
):
    run_id = _insert_run(automation_id)
    seed_external(world, automation_id, VALID["namespace"])
    _delete(automation_id, world, complete=cascade_finished)

    run = run_finalization.finalize_terminated_by_deletion(run_id)

    assert run.state == RunState.FAILED
    assert run.failure_class == "terminated_by_deletion"
    stored = _run_row(test_engine, run_id)
    assert stored.state == "failed"
    assert stored.failure_class == "terminated_by_deletion"
    assert stored.finished_at is not None
    assert stored.attempts == 2
    assert stored.automation_digest == DIGEST
    assert stored.outcome_status is None  # no fabricated wrapper outcome


def test_success_committed_during_teardown_is_preserved(test_engine, world, automation_id):
    run_id = _insert_run(automation_id)
    _delete(automation_id, world, complete=False)
    assert run_finalization.complete_run_if_open(
        run_id, state=RunState.SUCCEEDED, outcome_status="ok"
    )

    run = run_finalization.finalize_terminated_by_deletion(run_id)

    assert run.state == RunState.SUCCEEDED
    assert _run_row(test_engine, run_id).failure_class is None


def test_late_response_cannot_overwrite_deletion_termination(test_engine, world, automation_id):
    run_id = _insert_run(automation_id)
    _delete(automation_id, world)
    run_finalization.finalize_terminated_by_deletion(run_id)

    late = run_finalization.complete_run_if_open(
        run_id, state=RunState.SUCCEEDED, outcome_status="ok"
    )

    assert late is False
    assert _run_row(test_engine, run_id).failure_class == "terminated_by_deletion"


def test_pending_never_invoked_run_is_suppressed_not_deletion_killed(
    test_engine, world, automation_id
):
    run_id = _insert_run(automation_id, state=RunState.PENDING, attempts=None)
    _delete(automation_id, world)

    run = run_finalization.finalize_terminated_by_deletion(run_id)

    assert run.state == RunState.SUPPRESSED
    stored = _run_row(test_engine, run_id)
    assert stored.failure_class is None
    assert stored.suppression_reason is None


def test_run_of_live_automation_is_not_attributed_to_deletion(test_engine, automation_id):
    run_id = _insert_run(automation_id)
    with pytest.raises(AutomationLifecycleConflictError):
        run_finalization.finalize_terminated_by_deletion(run_id)
    assert _run_row(test_engine, run_id).state == "submitted"


def test_cascade_does_not_bulk_finalize_submitted_rows(test_engine, world, automation_id):
    """CAPP 204 is not proof a call failed — rows stay for their owner."""
    run_id = _insert_run(automation_id)
    _delete(automation_id, world)
    assert _run_row(test_engine, run_id).state == "submitted"


def test_unknown_run_is_not_found(test_engine):
    with pytest.raises(RunNotFoundError):
        run_finalization.finalize_terminated_by_deletion(uuid4())


def test_deletion_started_gate(world, automation_id, test_engine):
    assert run_finalization.deletion_started(automation_id) is False
    _delete(automation_id, world, complete=False)
    assert run_finalization.deletion_started(automation_id) is True
    with pytest.raises(AutomationNotFoundError):
        run_finalization.deletion_started(uuid4())


# --- build fencing ------------------------------------------------------


@pytest.mark.parametrize("complete", [False, True], ids=["deleting", "deleted"])
def test_late_build_mutation_after_delete_is_a_no_op(test_engine, world, automation_id, complete):
    _delete(automation_id, world, complete=complete)
    with get_session() as session:
        assert run_finalization.lock_for_build_mutation(session, automation_id) is None


def test_build_mutation_lock_makes_delete_wait_then_409(test_engine, world, automation_id):
    held = threading.Event()
    release = threading.Event()

    def build_cutover():
        with get_session() as session:
            automation = run_finalization.lock_for_build_mutation(session, automation_id)
            assert automation is not None
            held.set()
            release.wait(timeout=10)
            automation.build_state = BuildState.BUILDING
            session.add(automation)
            session.commit()

    outcome = {}

    def delete():
        try:
            _delete(automation_id, world, complete=False)
            outcome["result"] = "admitted"
        except AutomationBuildingError:
            outcome["result"] = "409"

    builder = threading.Thread(target=build_cutover)
    builder.start()
    assert held.wait(timeout=10)
    deleter = threading.Thread(target=delete)
    deleter.start()
    time.sleep(0.3)
    assert "result" not in outcome
    release.set()
    builder.join(timeout=10)
    deleter.join(timeout=10)

    assert outcome["result"] == "409"
    assert row(test_engine, automation_id).matching_state == "inactive"
    assert world.calls == []
