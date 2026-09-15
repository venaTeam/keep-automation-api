"""Create/edit never hold a row lock or pooled connection across git I/O (D14-ready).

Each fake git client runs its assertions *during* `commit_script` — the exact
window a real GitLab call would occupy.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import text

from src import config
from src.bl import automations_bl, cascade, lifecycle
from src.bl.git_client import InMemoryGitClient
from src.exceptions import (
    AutomationBuildingError,
    AutomationEditSupersededError,
)
from src.models.api.automation import AutomationIn
from src.models.db.automation import BuildState, MatchingState
from tests.conftest import TENANT
from tests.fakes import World, deps_for
from tests.helpers import DIGEST, VALID, revisions, row


class ProbingGit(InMemoryGitClient):
    """Runs `probe()` inside commit_script, then optionally fails."""

    def __init__(self, probe=None, fail: Exception | None = None):
        super().__init__()
        self.probe = probe
        self.fail = fail

    def commit_script(self, script_path, content, message):
        if self.probe:
            self.probe()
        if self.fail:
            raise self.fail
        return super().commit_script(script_path, content, message)


def _create_idle(git=None, **columns) -> UUID:
    automation = automations_bl.create_automation(
        TENANT, AutomationIn(**VALID), actor="alice@keep", git=git or InMemoryGitClient()
    )
    from src.core.db import get_engine

    assignments = {"build_state": "idle", "build_lock_deadline": None, **columns}
    set_clause = ", ".join(f"{c} = :{c}" for c in assignments)
    with get_engine().begin() as conn:
        conn.execute(
            text(f"UPDATE automations SET {set_clause} WHERE id = :id"),
            {**assignments, "id": str(automation.id)},
        )
    return automation.id


def _row_is_unlocked(engine, automation_id) -> bool:
    with engine.connect() as conn:
        with conn.begin():
            locked = conn.execute(
                text(
                    "SELECT id FROM automations WHERE id = :id "
                    "FOR UPDATE SKIP LOCKED"
                ),
                {"id": str(automation_id)},
            ).first()
    return locked is not None


# --- create ---------------------------------------------------------------


def test_create_commits_git_with_no_connection_checked_out(test_engine):
    seen = {}
    git = ProbingGit(probe=lambda: seen.update(checked_out=test_engine.pool.checkedout()))

    automation = automations_bl.create_automation(
        TENANT, AutomationIn(**VALID), actor="alice@keep", git=git
    )

    assert seen["checked_out"] == 0
    stored = row(test_engine, automation.id)
    sha = revisions(test_engine, automation.id)[0].git_sha
    assert stored.building_sha == sha
    remaining = stored.build_lock_deadline - datetime.now(timezone.utc)
    assert timedelta(seconds=config.BUILD_LOCK_TIMEOUT_SECONDS - 60) < remaining
    assert remaining <= timedelta(seconds=config.BUILD_LOCK_TIMEOUT_SECONDS)


def test_create_git_failure_leaves_no_rows(test_engine):
    with pytest.raises(RuntimeError):
        automations_bl.create_automation(
            TENANT,
            AutomationIn(**VALID),
            actor="alice@keep",
            git=ProbingGit(fail=RuntimeError("gitlab down")),
        )
    with test_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM automations")).scalar() == 0


# --- edit -----------------------------------------------------------------


def test_edit_holds_no_lock_and_no_connection_during_git(test_engine):
    automation_id = _create_idle()
    seen = {}

    def probe():
        seen["checked_out"] = test_engine.pool.checkedout()
        seen["unlocked"] = _row_is_unlocked(test_engine, automation_id)
        seen["build_state"] = row(test_engine, automation_id).build_state

    automations_bl.update_automation(
        TENANT,
        automation_id,
        AutomationIn(**{**VALID, "name": "renamed"}),
        actor="bob@keep",
        git=ProbingGit(probe=probe),
    )

    assert seen == {"checked_out": 0, "unlocked": True, "build_state": "building"}


def test_delete_during_in_flight_edit_is_immediate_409_not_a_lock_wait(test_engine):
    automation_id = _create_idle()
    world = World()
    outcome = {}

    def probe():
        try:
            cascade.begin_delete(TENANT, automation_id, "del@keep", deps_for(world).publisher)
        except AutomationBuildingError:
            outcome["delete"] = "409"

    automations_bl.update_automation(
        TENANT, automation_id, AutomationIn(**VALID), actor="bob@keep", git=ProbingGit(probe=probe)
    )

    assert outcome == {"delete": "409"}
    assert row(test_engine, automation_id).matching_state == "inactive"


def test_toggle_during_in_flight_edit_is_not_blocked(test_engine):
    automation_id = _create_idle(active_digest=DIGEST)
    world = World()
    seen = {}

    def probe():
        enabled = lifecycle.enable_automation(
            TENANT, automation_id, "ops@keep", deps_for(world).publisher
        )
        seen["state"] = enabled.matching_state

    automations_bl.update_automation(
        TENANT, automation_id, AutomationIn(**VALID), actor="bob@keep", git=ProbingGit(probe=probe)
    )

    assert seen["state"] == MatchingState.ACTIVE
    # The edit's finish did not overwrite the toggle.
    assert row(test_engine, automation_id).matching_state == "active"


def test_edit_success_records_sha_deadline_and_revision(test_engine):
    automation_id = _create_idle()
    updated = automations_bl.update_automation(
        TENANT,
        automation_id,
        AutomationIn(**{**VALID, "name": "renamed"}),
        actor="bob@keep",
        git=InMemoryGitClient(),
    )
    edit = revisions(test_engine, automation_id)[-1]
    assert (edit.action, edit.actor) == ("edit", "bob@keep")
    assert updated.building_sha == edit.git_sha
    assert updated.build_state == BuildState.BUILDING
    assert updated.build_lock_deadline is not None
    assert updated.name == "renamed"


def test_edit_git_failure_restores_previous_build_state_exactly(test_engine):
    automation_id = _create_idle(build_state="build_failed", building_sha="old-sha")

    with pytest.raises(RuntimeError):
        automations_bl.update_automation(
            TENANT,
            automation_id,
            AutomationIn(**{**VALID, "name": "renamed"}),
            actor="bob@keep",
            git=ProbingGit(fail=RuntimeError("gitlab down")),
        )

    stored = row(test_engine, automation_id)
    assert stored.build_state == "build_failed"
    assert stored.building_sha == "old-sha"
    assert stored.build_lock_deadline is None
    assert stored.name == VALID["name"]
    assert [r.action for r in revisions(test_engine, automation_id)] == ["create"]


def test_edit_whose_claim_was_released_does_not_apply(test_engine):
    """E21 released the lock mid-commit: the stale edit must not write."""
    automation_id = _create_idle()

    def reconciler_releases_lock():
        with test_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE automations SET build_state = 'build_failed', "
                    "build_lock_deadline = NULL"
                )
            )

    with pytest.raises(AutomationEditSupersededError):
        automations_bl.update_automation(
            TENANT,
            automation_id,
            AutomationIn(**{**VALID, "name": "stale"}),
            actor="bob@keep",
            git=ProbingGit(probe=reconciler_releases_lock),
        )

    stored = row(test_engine, automation_id)
    assert stored.name == VALID["name"]
    assert stored.build_state == "build_failed"
    assert [r.action for r in revisions(test_engine, automation_id)] == ["create"]


def test_failed_edit_never_releases_a_newer_claim(test_engine):
    automation_id = _create_idle()
    newer_deadline = datetime.now(timezone.utc) + timedelta(hours=1)

    def newer_edit_claims():
        with test_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE automations SET build_state = 'building', "
                    "build_lock_deadline = :deadline, building_sha = 'newer'"
                ),
                {"deadline": newer_deadline},
            )

    with pytest.raises(RuntimeError):
        automations_bl.update_automation(
            TENANT,
            automation_id,
            AutomationIn(**VALID),
            actor="bob@keep",
            git=ProbingGit(probe=newer_edit_claims, fail=RuntimeError("gitlab down")),
        )

    stored = row(test_engine, automation_id)
    assert stored.build_state == "building"
    assert stored.building_sha == "newer"
    assert stored.build_lock_deadline == newer_deadline


def test_edit_via_http_uses_the_same_flow(client, test_engine):
    created = client.post("/automations", json=VALID).json()
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET build_state = 'idle'"))
    resp = client.put(f"/automations/{created['id']}", json={**VALID, "name": "renamed"})
    assert resp.status_code == 200
    assert row(test_engine, created["id"]).building_sha is not None
