"""Harness smoke test: the Postgres fixture is live and carries the whole
schema the automation tables depend on — including the `tenant` FK target,
which lives in keep-api-gateway and is stood in for here (see conftest)."""
from sqlalchemy import inspect, text

from tests.conftest import SEEDED_TENANTS


def test_automation_tables_exist_and_empty(test_engine):
    with test_engine.connect() as conn:
        for table in ("automations", "automation_runs", "automation_revisions"):
            count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            assert count == 0


def test_tenant_fk_target_exists_and_is_seeded(test_engine):
    with test_engine.connect() as conn:
        seeded = {row[0] for row in conn.execute(text("SELECT id FROM tenant"))}
    assert set(SEEDED_TENANTS) <= seeded


def test_tenant_id_is_a_real_not_null_foreign_key(test_engine):
    """The point of co-locating in the `keep` DB — an FK, not an advisory column."""
    inspector = inspect(test_engine)
    for table in ("automations", "automation_runs"):
        columns = {c["name"]: c for c in inspector.get_columns(table)}
        assert columns["tenant_id"]["nullable"] is False
        targets = {
            (fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in inspector.get_foreign_keys(table)
        }
        assert ("tenant", ("id",)) in targets

    # Revisions are reached only through their parent automation (spec §4.4).
    revision_columns = {c["name"] for c in inspector.get_columns("automation_revisions")}
    assert "tenant_id" not in revision_columns


def test_engine_injected_into_app_module(test_engine):
    import src.core.db as db_core

    assert db_core.get_engine() is test_engine
