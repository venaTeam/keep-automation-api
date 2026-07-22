"""BL tests: transactional create (git failure → zero rows), update lock, list cap."""
import pytest
from sqlalchemy import text

from src.bl import automations_bl
from src.bl.git_client import InMemoryGitClient
from src.exceptions import (
    AutomationBuildingError,
    AutomationNotFoundError,
    AutomationValidationError,
)
from src.models.api.automation import AutomationIn
from src.models.db.automation import BuildState, MatchingState

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
        AutomationIn(**VALID), actor="alice@keep", git=git
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
            AutomationIn(**VALID), actor="alice@keep", git=ExplodingGitClient()
        )
    assert row_counts(test_engine) == (0, 0)


def test_invalid_payload_raises_accumulated_errors(test_engine):
    bad = AutomationIn(**{**VALID, "triggers": [{"field": "message", "value": "x"}]})
    with pytest.raises(AutomationValidationError) as excinfo:
        automations_bl.create_automation(bad, actor="alice@keep", git=InMemoryGitClient())
    assert len(excinfo.value.errors) == 2  # too few + unknown field
    assert row_counts(test_engine) == (0, 0)


def test_update_rejected_while_building(test_engine):
    git = InMemoryGitClient()
    automation = automations_bl.create_automation(
        AutomationIn(**VALID), actor="alice@keep", git=git
    )
    assert automation.build_state == BuildState.BUILDING
    with pytest.raises(AutomationBuildingError):
        automations_bl.update_automation(
            automation.id, AutomationIn(**VALID), actor="bob@keep", git=git
        )


def test_update_after_build_completes(test_engine):
    git = InMemoryGitClient()
    automation = automations_bl.create_automation(
        AutomationIn(**VALID), actor="alice@keep", git=git
    )
    # Simulate the D15 webhook clearing the build lock.
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE automations SET build_state = 'idle'"))

    updated = automations_bl.update_automation(
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
        AutomationIn(**VALID), actor="alice@keep", git=git
    )
    automation, script = automations_bl.get_automation(created.id, git=git)
    assert automation.id == created.id
    assert script == VALID["script"]


def test_get_missing_raises_not_found(test_engine):
    from uuid import uuid4

    with pytest.raises(AutomationNotFoundError):
        automations_bl.get_automation(uuid4(), git=InMemoryGitClient())


def test_list_filters(test_engine):
    git = InMemoryGitClient()
    automations_bl.create_automation(AutomationIn(**VALID), actor="a@keep", git=git)
    automations_bl.create_automation(
        AutomationIn(**{**VALID, "name": "other", "namespace": "other-wallet"}),
        actor="a@keep",
        git=git,
    )
    assert len(automations_bl.list_automations()) == 2
    assert len(automations_bl.list_automations(namespace="other-wallet")) == 1
    assert len(automations_bl.list_automations(build_state=BuildState.BUILDING)) == 2
    assert automations_bl.list_automations(build_state=BuildState.IDLE) == []


def test_list_is_bounded():
    assert automations_bl.LIST_LIMIT == 1000
