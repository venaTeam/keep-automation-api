"""Delete cascade: order, checkpoints, restart matrix, failures, races (D18, spec §5.4)."""
import logging
import threading
import time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from src.bl import cascade
from src.exceptions import (
    AutomationBuildingError,
    AutomationLifecycleConflictError,
    AutomationNotFoundError,
)
from tests.conftest import OTHER_TENANT, TENANT
from tests.fakes import SimulatedFailure, World, deps_for, seed_external
from tests.helpers import VALID, create_built, revisions, row

WALLET = VALID["namespace"]
ACTOR = "deleter@keep"

ORDER = ["delete_capp", "delete_secret", "list_images", "archive"]


@pytest.fixture()
def world():
    return World()


@pytest.fixture()
def built(client, test_engine, world):
    """A built automation whose deploy left a Capp, run-auth Secret and images."""
    automation_id = create_built(client, test_engine)
    seed_external(world, automation_id, WALLET)
    # A team Secret in the same wallet, referenced by name — must always survive.
    world.secrets.add((WALLET, "team-secret"))
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET secret_name = 'team-secret'"))
    return UUID(automation_id)


def _begin(automation_id, world, tenant=TENANT):
    return cascade.begin_delete(tenant, automation_id, ACTOR, deps_for(world).publisher)


def _assert_fully_deleted(test_engine, world, automation_id):
    current = row(test_engine, automation_id)
    assert current.matching_state == "deleted"
    assert current.delete_cascade_step == 5
    assert (WALLET, f"automation-{automation_id}") not in world.capps
    assert (WALLET, f"automation-{automation_id}-run-auth") not in world.secrets
    assert world.images[str(automation_id)] == set()
    assert deps_for(world).git.archived(automation_id)
    # Retained forever: the row, its revisions, the script bytes, team Secret.
    assert (WALLET, "team-secret") in world.secrets
    assert [r.action for r in revisions(test_engine, automation_id)].count("delete") == 1


# --- happy path ---------------------------------------------------------


def test_cascade_runs_five_steps_in_exact_order(test_engine, world, built):
    admitted = _begin(built, world)
    assert admitted.matching_state == "deleting"
    assert admitted.delete_cascade_step == 1
    assert admitted.index_generation == 1

    result = cascade.run_cascade(built, deps_for(world))

    assert result.completed and result.error is None
    assert result.delete_cascade_step == 5
    ops = [op for op in world.operations() if op != "delete_image"]
    assert ops == ["publish_reload", "delete_capp", "delete_secret", "list_images", "list_images", "archive"]
    assert ("delete_capp", WALLET, f"automation-{built}") in world.calls
    assert ("delete_secret", WALLET, f"automation-{built}-run-auth") in world.calls
    assert ("archive", str(built), "noauth@keep") not in world.calls
    assert ("archive", str(built), ACTOR) in world.calls
    _assert_fully_deleted(test_engine, world, built)
    delete_revision = revisions(test_engine, built)[-1]
    assert (delete_revision.action, delete_revision.actor) == ("delete", ACTOR)


def test_step_one_publishes_after_commit(test_engine, world, built):
    seen = {}

    class SnapshotPublisher:
        def publish_reload(self):
            seen["state"] = row(test_engine, built).matching_state

    cascade.begin_delete(TENANT, built, ACTOR, SnapshotPublisher())
    assert seen["state"] == "deleting"


def test_stored_capp_deployment_id_is_used(client, test_engine, world):
    automation_id = UUID(create_built(client, test_engine, capp_deployment_id="capp-xyz"))
    seed_external(world, automation_id, WALLET, capp_name="capp-xyz")
    _begin(automation_id, world)
    cascade.run_cascade(automation_id, deps_for(world))
    assert ("delete_capp", WALLET, "capp-xyz") in world.calls
    assert (WALLET, "capp-xyz") not in world.capps


def test_never_deployed_automation_still_deletes_capp_and_run_auth_secret(
    client, test_engine, world
):
    """A half-finished first deploy can leave the Secret with no stored id."""
    automation_id = UUID(create_built(client, test_engine))
    assert row(test_engine, automation_id).capp_deployment_id is None
    world.secrets.add((WALLET, f"automation-{automation_id}-run-auth"))

    _begin(automation_id, world)
    result = cascade.run_cascade(automation_id, deps_for(world))

    assert result.completed
    assert world.operations()[1:3] == ["delete_capp", "delete_secret"]
    assert world.secrets == set()


def test_already_absent_resources_are_success(test_engine, world, client):
    """CAPP 404 / nothing in the registry: delete-if-exists."""
    automation_id = UUID(create_built(client, test_engine))
    _begin(automation_id, world)
    assert cascade.run_cascade(automation_id, deps_for(world)).completed


def test_only_this_automations_run_auth_secret_is_deleted(client, test_engine, world, built):
    other = UUID(create_built(client, test_engine, payload={**VALID, "name": "other"}))
    seed_external(world, other, WALLET)

    _begin(built, world)
    cascade.run_cascade(built, deps_for(world))

    assert (WALLET, f"automation-{other}-run-auth") in world.secrets
    assert (WALLET, f"automation-{other}") in world.capps
    assert world.images[str(other)] == {"sha256:1", "sha256:2"}
    assert (WALLET, "team-secret") in world.secrets


# --- restart matrix -----------------------------------------------------


def _fail_checkpoint_once(monkeypatch, completed_step):
    real = cascade._checkpoint
    state = {"armed": True}

    def flaky(automation_id, completed, **values):
        if completed == completed_step and state["armed"]:
            state["armed"] = False
            raise SimulatedFailure("db checkpoint lost")
        return real(automation_id, completed, **values)

    monkeypatch.setattr(cascade, "_checkpoint", flaky)


# (label, adapter failures, checkpoint to lose, expected step after the failed attempt)
INTERRUPTIONS = [
    ("after_step_1", {"delete_capp": 1}, None, 1),
    ("inside_step_2_after_2a", {"delete_secret": 1}, None, 1),
    ("inside_step_2_after_2b_response_lost", {"after_delete_secret": 1}, None, 1),
    ("step_2_checkpoint_lost", {}, 2, 1),
    ("after_step_2", {"list_images": 1}, None, 2),
    ("after_step_3", {"archive": 1}, None, 3),
    ("step_4_response_lost", {"after_archive": 1}, None, 3),
    ("after_step_4", {}, 5, 4),
]


@pytest.mark.parametrize(
    "label,failures,lost_checkpoint,expected_step",
    INTERRUPTIONS,
    ids=[i[0] for i in INTERRUPTIONS],
)
def test_interrupted_cascade_resumes_to_completion(
    monkeypatch, test_engine, world, built, label, failures, lost_checkpoint, expected_step
):
    _begin(built, world)
    world.failures.update(failures)
    if lost_checkpoint is not None:
        _fail_checkpoint_once(monkeypatch, lost_checkpoint)

    first = cascade.run_cascade(built, deps_for(world))

    assert not first.completed
    assert first.error is not None
    assert row(test_engine, built).matching_state == "deleting"
    assert row(test_engine, built).delete_cascade_step == expected_step
    if expected_step < 2:
        # No registry work until Capp AND run-auth Secret are gone (checkpoint 2).
        assert "list_images" not in world.operations()

    # A brand-new runner (fresh adapters, fresh sessions) over the same world.
    world.failures.clear()
    resumed = cascade.resume_delete(built, deps_for(world))

    assert resumed.completed and resumed.error is None
    _assert_fully_deleted(test_engine, world, built)
    assert deps_for(world).git.archive_commits(built) == 1
    ops = world.operations()
    firsts = [ops.index(op) for op in ORDER]
    assert firsts == sorted(firsts)


def test_resume_after_step_five_is_a_no_op(test_engine, world, built):
    _begin(built, world)
    cascade.run_cascade(built, deps_for(world))
    calls_before = len(world.calls)

    result = cascade.resume_delete(built, deps_for(world))

    assert result.completed
    assert len(world.calls) == calls_before


def test_partial_registry_deletion_repeats_safely(test_engine, world, built):
    _begin(built, world)
    world.failures["delete_image"] = 0

    class OneThenFail:
        def __init__(self, inner):
            self.inner = inner
            self.deleted = 0

        def list_image_digests(self, automation_id):
            return self.inner.list_image_digests(automation_id)

        def delete_image(self, automation_id, digest):
            if self.deleted == 1:
                raise SimulatedFailure("registry timeout")
            self.deleted += 1
            self.inner.delete_image(automation_id, digest)

    deps = deps_for(world)
    flaky = cascade.CascadeDeps(
        capp=deps.capp, registry=OneThenFail(deps.registry), git=deps.git, publisher=deps.publisher
    )
    first = cascade.run_cascade(built, flaky)
    assert first.error == "registry_delete_failed"
    assert first.delete_cascade_step == 2
    assert len(world.images[str(built)]) == 1

    assert cascade.resume_delete(built, deps_for(world)).completed
    assert world.images[str(built)] == set()


def test_registry_not_advanced_while_inventory_non_empty(test_engine, world, built):
    """An image pushed during deletion (late build) blocks the checkpoint."""
    _begin(built, world)
    deps = deps_for(world)

    class LatePush:
        def list_image_digests(self, automation_id):
            return deps.registry.list_image_digests(automation_id)

        def delete_image(self, automation_id, digest):
            deps.registry.delete_image(automation_id, digest)
            world.images[str(automation_id)].add("sha256:late")

    result = cascade.run_cascade(
        built, cascade.CascadeDeps(capp=deps.capp, registry=LatePush(), git=deps.git, publisher=deps.publisher)
    )
    assert result.error == "registry_inventory_not_empty"
    assert row(test_engine, built).delete_cascade_step == 2
    assert cascade.resume_delete(built, deps_for(world)).completed


# --- failures -----------------------------------------------------------


@pytest.mark.parametrize(
    "operation,code",
    [
        ("delete_capp", "capp_delete_failed"),
        ("delete_secret", "run_auth_secret_delete_failed"),
        ("list_images", "registry_list_failed"),
        ("delete_image", "registry_delete_failed"),
        ("archive", "git_archive_failed"),
    ],
)
def test_persistent_failure_stops_and_never_marks_deleted(
    test_engine, world, built, operation, code, caplog
):
    _begin(built, world)
    world.failures[operation] = -1

    with caplog.at_level(logging.WARNING):
        result = cascade.run_cascade(built, deps_for(world))
        again = cascade.resume_delete(built, deps_for(world))

    assert result.error == code and again.error == code
    assert row(test_engine, built).matching_state == "deleting"
    # Sanitized: the exception type is logged, never its message.
    assert "SimulatedFailure" in caplog.text
    assert "super-secret-value" not in caplog.text


def test_unconfigured_default_adapters_fail_closed(test_engine, world, built):
    from src.bl.cascade_adapters import UnconfiguredCappDeletionClient, UnconfiguredRegistryClient

    _begin(built, world)
    deps = deps_for(world)
    result = cascade.run_cascade(
        built,
        cascade.CascadeDeps(
            capp=UnconfiguredCappDeletionClient(),
            registry=UnconfiguredRegistryClient(),
            git=deps.git,
            publisher=deps.publisher,
        ),
    )
    assert result.error == "capp_delete_failed"
    assert row(test_engine, built).matching_state == "deleting"


def test_run_auth_key_cleared_only_with_checkpoint_two(monkeypatch, test_engine, world, client):
    """2c: the DB key goes in checkpoint 2's transaction, never before 2a+2b.

    The real column (`run_api_key_encrypted`) arrives with D16/F24; this points
    the same mechanism at an existing nullable column to prove the semantics.
    """
    monkeypatch.setattr(cascade, "RUN_AUTH_KEY_COLUMN", "deployment_url")
    automation_id = UUID(create_built(client, test_engine, deployment_url="https://run"))
    _begin(automation_id, world)

    world.failures["delete_secret"] = 1
    cascade.run_cascade(automation_id, deps_for(world))
    assert row(test_engine, automation_id).deployment_url == "https://run"

    _fail_checkpoint_once(monkeypatch, 2)
    cascade.run_cascade(automation_id, deps_for(world))
    assert row(test_engine, automation_id).deployment_url == "https://run"
    assert row(test_engine, automation_id).delete_cascade_step == 1

    cascade.run_cascade(automation_id, deps_for(world))
    assert row(test_engine, automation_id).deployment_url is None


def test_missing_run_auth_column_is_skipped():
    assert cascade.RUN_AUTH_KEY_COLUMN not in cascade.Automation.__table__.c
    assert cascade._run_auth_key_clear_values() == {}


# --- admission ----------------------------------------------------------


def test_delete_while_building_is_refused_with_no_side_effects(client, test_engine, world):
    created = client.post("/automations", json=VALID).json()  # building
    before = row(test_engine, created["id"])

    with pytest.raises(AutomationBuildingError):
        _begin(UUID(created["id"]), world)

    after = row(test_engine, created["id"])
    assert (after.matching_state, after.delete_cascade_step, after.index_generation) == (
        before.matching_state,
        before.delete_cascade_step,
        before.index_generation,
    )
    assert world.calls == []
    assert [r.action for r in revisions(test_engine, created["id"])] == ["create"]


def test_repeated_admission_does_not_rewrite_actor_generation_or_revision(test_engine, world, built):
    _begin(built, world)
    again = cascade.begin_delete(TENANT, built, "someone-else@keep", deps_for(world).publisher)
    assert again.updated_by == ACTOR
    assert again.index_generation == 1
    assert [r.action for r in revisions(test_engine, built)].count("delete") == 1
    assert world.publishes == 1


def test_deleting_row_with_building_state_follows_idempotent_path(test_engine, world, built):
    _begin(built, world)
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET build_state = 'building'"))
    assert _begin(built, world).matching_state == "deleting"


def test_cross_tenant_admission_is_not_found(client_as, test_engine, world):
    automation_id = UUID(create_built(client_as(OTHER_TENANT), test_engine))
    with pytest.raises(AutomationNotFoundError):
        _begin(automation_id, world, tenant=TENANT)
    assert row(test_engine, automation_id).matching_state == "inactive"


def test_resume_refuses_never_deleted_automation(test_engine, world, built):
    with pytest.raises(AutomationLifecycleConflictError):
        cascade.resume_delete(built, deps_for(world))
    assert world.calls == []
    assert row(test_engine, built).matching_state == "inactive"


def test_resume_unknown_is_not_found(world, test_engine):
    with pytest.raises(AutomationNotFoundError):
        cascade.resume_delete(uuid4(), deps_for(world))


def test_resume_of_deleting_row_without_checkpoint_records_step_one(test_engine, world, built):
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET matching_state = 'deleting'"))
    result = cascade.resume_delete(built, deps_for(world))
    assert result.completed
    assert world.operations()[0] == "publish_reload"


# --- concurrency --------------------------------------------------------


def test_two_concurrent_runners_converge_without_regressing(test_engine, world, built):
    _begin(built, world)
    barrier = threading.Barrier(2, timeout=10)
    steps_seen: list[int] = []
    real_checkpoint = cascade._checkpoint

    def recording_checkpoint(automation_id, completed, **values):
        won = real_checkpoint(automation_id, completed, **values)
        steps_seen.append(row(test_engine, automation_id).delete_cascade_step)
        return won

    class RendezvousCapp:
        def __init__(self, inner):
            self.inner = inner

        def delete_capp(self, wallet, name):
            barrier.wait()  # both runners are inside step 2 at once
            self.inner.delete_capp(wallet, name)

        def delete_secret(self, wallet, name):
            self.inner.delete_secret(wallet, name)

    results = []

    def runner():
        deps = deps_for(world)
        results.append(
            cascade.run_cascade(
                built,
                cascade.CascadeDeps(
                    capp=RendezvousCapp(deps.capp),
                    registry=deps.registry,
                    git=deps.git,
                    publisher=deps.publisher,
                ),
            )
        )

    original = cascade._checkpoint
    cascade._checkpoint = recording_checkpoint
    try:
        threads = [threading.Thread(target=runner) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        cascade._checkpoint = original

    assert len(results) == 2 and all(r.completed for r in results)
    assert steps_seen == sorted(steps_seen)  # never observed going backward
    _assert_fully_deleted(test_engine, world, built)
    assert deps_for(world).git.archive_commits(built) == 1


def test_delete_admission_waits_for_a_build_admitted_under_the_row_lock(
    test_engine, world, built
):
    """Either the build wins (DELETE -> 409) or delete wins — never both."""
    locked = threading.Event()
    release = threading.Event()

    def build_admission():
        with test_engine.begin() as conn:
            conn.execute(
                text("SELECT id FROM automations WHERE id = :id FOR UPDATE"),
                {"id": str(built)},
            )
            locked.set()
            release.wait(timeout=10)
            conn.execute(text("UPDATE automations SET build_state = 'building'"))

    builder = threading.Thread(target=build_admission)
    builder.start()
    assert locked.wait(timeout=10)

    outcome = {}

    def delete_admission():
        try:
            _begin(built, world)
            outcome["result"] = "admitted"
        except AutomationBuildingError:
            outcome["result"] = "409"

    deleter = threading.Thread(target=delete_admission)
    deleter.start()
    time.sleep(0.3)
    assert "result" not in outcome  # blocked on the row lock
    release.set()
    builder.join(timeout=10)
    deleter.join(timeout=10)

    assert outcome["result"] == "409"
    assert row(test_engine, built).matching_state == "inactive"


def test_edit_after_delete_admission_is_refused(client, test_engine, world, built):
    _begin(built, world)
    resp = client.put(f"/automations/{built}", json=VALID)
    assert resp.status_code == 409
    assert row(test_engine, built).build_state == "idle"
