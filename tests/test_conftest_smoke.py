"""Harness smoke test: Postgres fixture is live and at the A1 schema head."""
from sqlalchemy import text


def test_automation_tables_exist_and_empty(test_engine):
    with test_engine.connect() as conn:
        for table in ("automations", "automation_runs", "automation_revisions"):
            count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            assert count == 0


def test_engine_injected_into_app_module(test_engine):
    import src.core.db as db_core

    assert db_core.get_engine() is test_engine
