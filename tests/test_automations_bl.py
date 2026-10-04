"""BL tests: transactional create (git failure → zero rows), update lock, list
cap, and tenant scoping (a tenant only ever sees its own rows)."""
import pytest
from sqlalchemy import text

from src.bl import automations_bl
from src.bl.git_client import InMemoryGitClient
from src.exceptions import (
    AutomationBuildingError,
    AutomationNotFoundError,
    AutomationStateConflictError,
    AutomationValidationError,
)
from src.models.api.automation import AutomationIn
from src.models.db.automation import BuildState, MatchingState
from tests.conftest import OTHER_TENANT, TENANT

VALID = dict(
    name="restart-payments",
    namespace="payments-wallet",
    script="def handle(alert):\n    return {}\n",
    triggers=[
        {"field": "severity", "value": "critical"},
        {"field": "application", "value": "payments"},
    ],
)


class ExplodingGitClient(InMemoryGitClient):
    def commit_script(self, script_path, content, message):
        raise RuntimeError("git unavailable")


def row_counts(engine):
    with engine.connect() as conn:
        automations = conn.execute(text("SELECT COUNT(*) FROM automations")).scalar()
        revisions = conn.execute(
            text("SELECT COUNT(*) FROM automation_revisions")
        ).scalar()
    return automations, revisions


def test_create_writes_both_rows_and_commits_script(test_engine):
    git = InMemoryGitClient()
    automation = automations_bl.create_automation(
        TENANT, AutomationIn(**VALID), actor="alice@keep", git=git
    )
    assert automation.matching_state == MatchingState.INACTIVE
    assert automation.build_state == BuildState.BUILDING
    assert automation.timeout_seconds == 300  # default applied
    assert automation.grace_seconds == 300
    assert row_counts(test_engine) == (1, 1)
    assert git.read_script(automation.script_path) == VALID["script"]
    with test_engine.connect() as conn:
        actor, action, sha = conn.execute(
            text("SELECT actor, action, git_sha FROM automation_revisions")
        ).one()
    assert actor == "alice@keep"
    assert action == "create"
    assert sha is not None


def test_git_failure_rolls_back_both_rows(test_engine):
    with pytest.raises(RuntimeError):
        automations_bl.create_automation(
            TENANT, AutomationIn(**VALID), actor="alice@keep", git=ExplodingGitClient()
        )
    assert row_counts(test_engine) == (0, 0)


def test_git_failure_on_update_rolls_back_row_changes(test_engine):
    git = InMemoryGitClient()
    automation = automations_bl.create_automation(
        TENANT, AutomationIn(**VALID), actor="alice@keep", git=git
    )
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET build_state = 'idle'"))

    with pytest.raises(RuntimeError):
        automations_bl.update_automation(
            TENANT,
            automation.id,
            AutomationIn(**{**VALID, "name": "renamed"}),
            actor="bob@keep",
            git=ExplodingGitClient(),
        )
    # No edit revision, name unchanged, build lock not re-armed.
    assert row_counts(test_engine) == (1, 1)
    with test_engine.connect() as conn:
        name, build_state = conn.execute(
            text("SELECT name, build_state FROM automations")
        ).one()
    assert name == VALID["name"]
    assert build_state == "idle"


def test_invalid_payload_raises_accumulated_errors(test_engine):
    bad = AutomationIn(**{**VALID, "triggers": [{"field": "message", "value": "x"}]})
    with pytest.raises(AutomationValidationError) as excinfo:
        automations_bl.create_automation(
            TENANT, bad, actor="alice@keep", git=InMemoryGitClient()
        )
    assert len(excinfo.value.errors) == 2  # too few + unknown field
    assert row_counts(test_engine) == (0, 0)


def test_update_rejected_while_building(test_engine):
    git = InMemoryGitClient()
    automation = automations_bl.create_automation(
        TENANT, AutomationIn(**VALID), actor="alice@keep", git=git
    )
    assert automation.build_state == BuildState.BUILDING
    with pytest.raises(AutomationBuildingError):
        automations_bl.update_automation(
            TENANT, automation.id, AutomationIn(**VALID), actor="bob@keep", git=git
        )


def test_update_after_build_completes(test_engine):
    git = InMemoryGitClient()
    automation = automations_bl.create_automation(
        TENANT, AutomationIn(**VALID), actor="alice@keep", git=git
    )
    # Simulate the D15 webhook clearing the build lock.
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET build_state = 'idle'"))

    updated = automations_bl.update_automation(
        TENANT,
        automation.id,
        AutomationIn(**{**VALID, "name": "restart-payments-v2", "script": "def handle(a): ...\n"}),
        actor="bob@keep",
        git=git,
    )
    assert updated.name == "restart-payments-v2"
    assert updated.updated_by == "bob@keep"
    assert updated.build_state == BuildState.BUILDING  # edit re-arms the build axis
    assert git.read_script(automation.script_path) == "def handle(a): ...\n"
    assert row_counts(test_engine) == (1, 2)  # create + edit revisions


def test_get_returns_script_read_back(test_engine):
    git = InMemoryGitClient()
    created = automations_bl.create_automation(
        TENANT, AutomationIn(**VALID), actor="alice@keep", git=git
    )
    automation, script = automations_bl.get_automation(TENANT, created.id, git=git)
    assert automation.id == created.id
    assert script == VALID["script"]


def test_get_missing_raises_not_found(test_engine):
    from uuid import uuid4

    with pytest.raises(AutomationNotFoundError):
        automations_bl.get_automation(TENANT, uuid4(), git=InMemoryGitClient())


def test_list_filters(test_engine):
    git = InMemoryGitClient()
    automations_bl.create_automation(
        TENANT, AutomationIn(**VALID), actor="a@keep", git=git
    )
    automations_bl.create_automation(
        TENANT,
        AutomationIn(**{**VALID, "name": "other", "namespace": "other-wallet"}),
        actor="a@keep",
        git=git,
    )
    assert len(automations_bl.list_automations(TENANT)) == 2
    assert len(automations_bl.list_automations(TENANT, namespace="other-wallet")) == 1
    assert (
        len(automations_bl.list_automations(TENANT, build_state=BuildState.BUILDING))
        == 2
    )
    assert automations_bl.list_automations(TENANT, build_state=BuildState.IDLE) == []


def test_list_is_bounded():
    assert automations_bl.LIST_LIMIT == 1000


# --- enable / disable / delete lifecycle --------------------------------


def _revision_actions(engine, automation_id):
    with engine.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT action FROM automation_revisions "
                    "WHERE automation_id = :id ORDER BY created_at"
                ),
                {"id": str(automation_id)},
            ).all()
        ]


def _engine_now():
    # The BL reaches the DB through the injected module global; tests that need
    # a raw connection use the same engine the test_engine fixture installed.
    import src.core.db as db_core

    return db_core._engine


def _built(git):
    """A created automation with the build lock cleared (as the D15 webhook would)."""
    a = automations_bl.create_automation(
        TENANT, AutomationIn(**VALID), actor="alice@keep", git=git
    )
    with _engine_now().begin() as conn:
        conn.execute(
            text("UPDATE automations SET build_state = 'idle' WHERE id = :id"),
            {"id": str(a.id)},
        )
    return a


def test_enable_activates_and_records_revision(test_engine):
    git = InMemoryGitClient()
    a = _built(git)
    enabled = automations_bl.enable_automation(TENANT, a.id, actor="bob@keep")
    assert enabled.matching_state == MatchingState.ACTIVE
    assert enabled.updated_by == "bob@keep"
    assert _revision_actions(test_engine, a.id) == ["create", "enable"]
    with test_engine.connect() as conn:
        sha = conn.execute(
            text(
                "SELECT git_sha FROM automation_revisions WHERE action = 'enable'"
            )
        ).scalar()
    assert sha is None  # lifecycle actions commit no script bytes


def test_enable_rejected_while_building(test_engine):
    git = InMemoryGitClient()
    a = automations_bl.create_automation(
        TENANT, AutomationIn(**VALID), actor="alice@keep", git=git
    )  # stays build_state=building
    with pytest.raises(AutomationBuildingError):
        automations_bl.enable_automation(TENANT, a.id, actor="bob@keep")


def test_enable_rejected_when_build_failed(test_engine):
    git = InMemoryGitClient()
    a = automations_bl.create_automation(
        TENANT, AutomationIn(**VALID), actor="alice@keep", git=git
    )
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET build_state = 'build_failed'"))
    with pytest.raises(AutomationStateConflictError):
        automations_bl.enable_automation(TENANT, a.id, actor="bob@keep")


def test_enable_is_idempotent(test_engine):
    git = InMemoryGitClient()
    a = _built(git)
    automations_bl.enable_automation(TENANT, a.id, actor="bob@keep")
    again = automations_bl.enable_automation(TENANT, a.id, actor="carol@keep")
    assert again.matching_state == MatchingState.ACTIVE
    # No second enable revision, and updated_by not churned by the no-op.
    assert _revision_actions(test_engine, a.id) == ["create", "enable"]
    assert again.updated_by == "bob@keep"


def test_disable_deactivates_and_records_revision(test_engine):
    git = InMemoryGitClient()
    a = _built(git)
    automations_bl.enable_automation(TENANT, a.id, actor="bob@keep")
    disabled = automations_bl.disable_automation(TENANT, a.id, actor="carol@keep")
    assert disabled.matching_state == MatchingState.INACTIVE
    assert _revision_actions(test_engine, a.id) == ["create", "enable", "disable"]


def test_disable_is_idempotent_when_inactive(test_engine):
    git = InMemoryGitClient()
    a = _built(git)  # inactive
    automations_bl.disable_automation(TENANT, a.id, actor="bob@keep")
    assert _revision_actions(test_engine, a.id) == ["create"]  # no-op, no revision


def test_delete_archives_and_records_revision(test_engine):
    git = InMemoryGitClient()
    a = automations_bl.create_automation(
        TENANT, AutomationIn(**VALID), actor="alice@keep", git=git
    )  # building — delete is allowed regardless
    deleted = automations_bl.delete_automation(TENANT, a.id, actor="bob@keep")
    assert deleted.matching_state == MatchingState.DELETING
    assert _revision_actions(test_engine, a.id) == ["create", "delete"]
    # Archive-mark, never erase: the row still exists.
    assert row_counts(test_engine)[0] == 1


def test_delete_is_idempotent(test_engine):
    git = InMemoryGitClient()
    a = _built(git)
    automations_bl.delete_automation(TENANT, a.id, actor="bob@keep")
    automations_bl.delete_automation(TENANT, a.id, actor="carol@keep")
    assert _revision_actions(test_engine, a.id) == ["create", "delete"]


def test_enable_rejected_when_archived(test_engine):
    git = InMemoryGitClient()
    a = _built(git)
    automations_bl.delete_automation(TENANT, a.id, actor="bob@keep")
    with pytest.raises(AutomationStateConflictError):
        automations_bl.enable_automation(TENANT, a.id, actor="bob@keep")


def test_lifecycle_actions_are_tenant_scoped(test_engine):
    git = InMemoryGitClient()
    theirs = automations_bl.create_automation(
        OTHER_TENANT, AutomationIn(**VALID), actor="a@keep", git=git
    )
    for action in (
        automations_bl.enable_automation,
        automations_bl.disable_automation,
        automations_bl.delete_automation,
    ):
        with pytest.raises(AutomationNotFoundError):
            action(TENANT, theirs.id, actor="attacker@keep")
    # The other tenant's row is untouched.
    with test_engine.connect() as conn:
        state = conn.execute(
            text("SELECT matching_state FROM automations WHERE id = :id"),
            {"id": str(theirs.id)},
        ).scalar()
    assert state == "inactive"


# --- tenant isolation ---------------------------------------------------
#
# Every test below creates a REAL row under each tenant, so a passing result
# means "the other tenant's row was filtered out", not "the other tenant had
# nothing to return" — the failure mode a leak actually looks like.


def _create(tenant_id: str, git: InMemoryGitClient, **overrides):
    return automations_bl.create_automation(
        tenant_id, AutomationIn(**{**VALID, **overrides}), actor="a@keep", git=git
    )


def test_create_stamps_the_caller_tenant(test_engine):
    git = InMemoryGitClient()
    created = _create(TENANT, git)
    with test_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT tenant_id FROM automations WHERE id = :id"),
            {"id": str(created.id)},
        ).scalar()
    assert stored == TENANT
    assert created.tenant_id == TENANT


def test_list_returns_only_the_callers_tenant(test_engine):
    git = InMemoryGitClient()
    mine = _create(TENANT, git)
    theirs = _create(OTHER_TENANT, git, name="theirs")

    assert [a.id for a in automations_bl.list_automations(TENANT)] == [mine.id]
    assert [a.id for a in automations_bl.list_automations(OTHER_TENANT)] == [theirs.id]
    # Both rows really are in the table — the filter is doing the work.
    assert row_counts(test_engine) == (2, 2)


def test_list_filters_do_not_widen_past_the_tenant(test_engine):
    git = InMemoryGitClient()
    _create(OTHER_TENANT, git, namespace="shared-wallet")
    # Same namespace, different tenant: the namespace filter must not reach it.
    assert automations_bl.list_automations(TENANT, namespace="shared-wallet") == []
    assert (
        len(automations_bl.list_automations(OTHER_TENANT, namespace="shared-wallet"))
        == 1
    )


def test_get_across_tenants_is_not_found(test_engine):
    git = InMemoryGitClient()
    theirs = _create(OTHER_TENANT, git)
    with pytest.raises(AutomationNotFoundError):
        automations_bl.get_automation(TENANT, theirs.id, git=git)
    # Same id, right tenant: proves the id itself is valid, so the 404 above is
    # the tenant predicate and not a mistyped lookup.
    found, _ = automations_bl.get_automation(OTHER_TENANT, theirs.id, git=git)
    assert found.id == theirs.id


def test_update_across_tenants_is_not_found_and_changes_nothing(test_engine):
    git = InMemoryGitClient()
    theirs = _create(OTHER_TENANT, git)
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET build_state = 'idle'"))

    with pytest.raises(AutomationNotFoundError):
        automations_bl.update_automation(
            TENANT,
            theirs.id,
            AutomationIn(**{**VALID, "name": "hijacked"}),
            actor="attacker@keep",
            git=git,
        )
    with test_engine.connect() as conn:
        name, updated_by = conn.execute(
            text("SELECT name, updated_by FROM automations WHERE id = :id"),
            {"id": str(theirs.id)},
        ).one()
    assert name == VALID["name"]
    assert updated_by == "a@keep"


def test_cross_tenant_update_is_404_before_the_build_lock_is_consulted(test_engine):
    """A mid-build row of another tenant must still read as absent, not 409.

    409 would confirm the id exists — the existence leak §8.1 forbids.
    """
    git = InMemoryGitClient()
    theirs = _create(OTHER_TENANT, git)
    assert theirs.build_state == BuildState.BUILDING
    with pytest.raises(AutomationNotFoundError):
        automations_bl.update_automation(
            TENANT, theirs.id, AutomationIn(**VALID), actor="attacker@keep", git=git
        )


def test_tenant_id_in_the_payload_is_ignored(test_engine):
    git = InMemoryGitClient()
    created = automations_bl.create_automation(
        TENANT,
        AutomationIn(**{**VALID, "tenant_id": OTHER_TENANT}),
        actor="a@keep",
        git=git,
    )
    assert created.tenant_id == TENANT
    assert not hasattr(AutomationIn(**VALID), "tenant_id")
    assert automations_bl.list_automations(OTHER_TENANT) == []


def test_unknown_tenant_is_rejected_by_the_foreign_key(test_engine):
    """The column is a real FK, so an unstamped/bogus tenant cannot be written."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        _create("no-such-tenant", InMemoryGitClient())
    assert row_counts(test_engine) == (0, 0)
