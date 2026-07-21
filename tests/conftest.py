"""DB test fixtures: fresh database + alembic upgrade + event-handler role.

Requires a reachable Postgres superuser (docker-compose.infra.yml locally, the
postgres service in CI). DB-marked tests are skipped when it is unreachable so
the plain app tests still run standalone.
"""
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parents[1]

SUPERUSER_URL = os.environ.get(
    "TEST_DATABASE_SUPERUSER_URL",
    "postgresql+psycopg2://postgres:postgres@127.0.0.1:5434/postgres",
)
TEST_DB_NAME = "keep_automations_test"
EH_ROLE = "test_event_handler_ro"
EH_PASSWORD = "test_event_handler_ro"


def _server_reachable() -> bool:
    try:
        engine = sa.create_engine(SUPERUSER_URL, poolclass=sa.pool.NullPool)
        with engine.connect():
            return True
    except sa.exc.OperationalError:
        return False


def _test_db_url(role: str = "postgres", password: str = "postgres") -> str:
    base = sa.engine.make_url(SUPERUSER_URL)
    # str(URL) masks the password as "***"; render it verbatim.
    return base.set(
        username=role, password=password, database=TEST_DB_NAME
    ).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def migrated_db_url():
    """Fresh test database with the event-handler role and migrations applied."""
    if not _server_reachable():
        pytest.skip(f"Postgres not reachable at {SUPERUSER_URL}")

    admin_engine = sa.create_engine(
        SUPERUSER_URL, poolclass=sa.pool.NullPool, isolation_level="AUTOCOMMIT"
    )
    with admin_engine.connect() as conn:
        conn.execute(
            sa.text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
        )
        conn.execute(sa.text(f"CREATE DATABASE {TEST_DB_NAME}"))
        conn.execute(sa.text(f'DROP ROLE IF EXISTS "{EH_ROLE}"'))
        conn.execute(
            sa.text(f"CREATE ROLE \"{EH_ROLE}\" LOGIN PASSWORD '{EH_PASSWORD}'")
        )

    db_url = _test_db_url()
    os.environ["DATABASE_URL"] = db_url
    os.environ["DATABASE_EVENT_HANDLER_ROLE"] = EH_ROLE
    command.upgrade(alembic_config(db_url), "head")
    return db_url


def alembic_config(db_url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture(scope="session")
def db_engine(migrated_db_url):
    engine = sa.create_engine(migrated_db_url, poolclass=sa.pool.NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def eh_role_engine(migrated_db_url):
    """Engine connected as the SELECT-only event-handler role."""
    engine = sa.create_engine(
        _test_db_url(EH_ROLE, EH_PASSWORD), poolclass=sa.pool.NullPool
    )
    yield engine
    engine.dispose()
