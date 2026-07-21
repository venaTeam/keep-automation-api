"""A1 verification: migrations, idempotency constraint, grants, reconciler index."""
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from tests.conftest import alembic_config

EXPECTED_TABLES = {"automations", "automation_runs", "automation_revisions"}


def _insert_automation(conn) -> uuid.UUID:
    automation_id = uuid.uuid4()
    conn.execute(
        sa.text(
            """
            INSERT INTO automations
                (id, name, namespace, triggers, script_path, created_by, updated_by)
            VALUES
                (:id, 'test', 'test-wallet',
                 '[{"field": "application", "value": "keep"}]'::jsonb,
                 :path, 'tester', 'tester')
            """
        ),
        {"id": str(automation_id), "path": f"/{automation_id}/script.py"},
    )
    return automation_id


def _insert_run(conn, automation_id, history_id: str):
    conn.execute(
        sa.text(
            """
            INSERT INTO automation_runs
                (run_id, automation_id, history_id, fingerprint, payload, matched_m)
            VALUES
                (:run_id, :automation_id, :history_id, 'fp-1', '{}'::jsonb, 1)
            """
        ),
        {
            "run_id": str(uuid.uuid4()),
            "automation_id": str(automation_id),
            "history_id": history_id,
        },
    )


def test_upgrade_head_creates_schema(db_engine):
    inspector = sa.inspect(db_engine)
    assert EXPECTED_TABLES <= set(inspector.get_table_names())


def test_upgrade_rerun_is_noop(migrated_db_url, db_engine):
    command.upgrade(alembic_config(migrated_db_url), "head")
    with db_engine.connect() as conn:
        version = conn.execute(sa.text("SELECT version_num FROM alembic_version"))
        script = ScriptDirectory.from_config(alembic_config(migrated_db_url))
        assert version.scalar() == script.get_current_head()


def test_duplicate_history_automation_rejected(db_engine):
    with db_engine.begin() as conn:
        automation_id = _insert_automation(conn)
        _insert_run(conn, automation_id, "evt-dup")
    with pytest.raises(sa.exc.IntegrityError) as excinfo:
        with db_engine.begin() as conn:
            _insert_run(conn, automation_id, "evt-dup")
    assert "uq_automation_runs_history_automation" in str(excinfo.value)


def test_same_history_different_automation_allowed(db_engine):
    with db_engine.begin() as conn:
        first = _insert_automation(conn)
        second = _insert_automation(conn)
        _insert_run(conn, first, "evt-fanout")
        _insert_run(conn, second, "evt-fanout")


def test_event_handler_role_can_select_automations(eh_role_engine):
    with eh_role_engine.connect() as conn:
        conn.execute(sa.text("SELECT id, triggers, matching_state FROM automations"))


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO automations (id, name, namespace, triggers, script_path,"
        " created_by, updated_by) VALUES (gen_random_uuid(), 'x', 'w',"
        " '[]'::jsonb, '/x/script.py', 'x', 'x')",
        "UPDATE automations SET name = 'hacked'",
        "DELETE FROM automations",
    ],
    ids=["insert", "update", "delete"],
)
def test_event_handler_role_cannot_write_automations(eh_role_engine, statement):
    with pytest.raises(sa.exc.ProgrammingError) as excinfo:
        with eh_role_engine.begin() as conn:
            conn.execute(sa.text(statement))
    assert "permission denied" in str(excinfo.value)


def test_event_handler_role_cannot_read_other_tables(eh_role_engine):
    for table in ("automation_runs", "automation_revisions"):
        with pytest.raises(sa.exc.ProgrammingError):
            with eh_role_engine.connect() as conn:
                conn.execute(sa.text(f"SELECT * FROM {table}"))


def test_reconciler_scan_uses_state_created_at_index(db_engine):
    with db_engine.connect() as conn:
        # Tiny tables make the planner prefer a seq scan; disabling it proves the
        # index exists and matches the reconciler scan shape.
        conn.execute(sa.text("SET enable_seqscan = off"))
        plan = "\n".join(
            row[0]
            for row in conn.execute(
                sa.text(
                    "EXPLAIN SELECT run_id FROM automation_runs"
                    " WHERE state = 'pending' AND created_at < now() - interval '120 seconds'"
                )
            )
        )
    assert "ix_automation_runs_state_created_at" in plan


def test_downgrade_to_base_and_back(migrated_db_url):
    cfg = alembic_config(migrated_db_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
