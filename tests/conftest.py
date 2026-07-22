"""Test harness: real Postgres (pytest-docker) + Alembic head + engine injection.

SQLite is not an option — the A1 schema uses JSONB, native PG enums
(`src/models/db/helpers.enum_column`) and UUID PKs. Tests therefore run against
a disposable Postgres container (`tests/docker-compose.test.yml`), or against
an existing empty database when `TEST_DATABASE_URL` is set (CI escape hatch).

The app and BL reach the DB through the lazy singleton in `src/core/db.py`;
the `test_engine` fixture injects the test engine into that module global so
every code path (routes, BL, direct sessions) hits the test database.
"""
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import src.core.db as db_core
from src.main import get_app

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def docker_compose_file():
    return str(REPO_ROOT / "tests" / "docker-compose.test.yml")


def _can_connect(url: str) -> bool:
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        with engine.connect():
            return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def database_url(request) -> str:
    override = os.environ.get("TEST_DATABASE_URL")
    if override:
        return override
    docker_ip = request.getfixturevalue("docker_ip")
    docker_services = request.getfixturevalue("docker_services")
    port = docker_services.port_for("postgres-test", 5432)
    url = f"postgresql+psycopg2://postgres:postgres@{docker_ip}:{port}/automations_test"
    docker_services.wait_until_responsive(
        timeout=60.0, pause=1.0, check=lambda: _can_connect(url)
    )
    return url


@pytest.fixture(scope="session")
def test_engine(database_url):
    alembic_cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    alembic_cfg.attributes["sqlalchemy_url"] = database_url
    command.upgrade(alembic_cfg, "head")

    engine = create_engine(database_url)
    previous = db_core._engine
    db_core._engine = engine
    yield engine
    db_core._engine = previous
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(test_engine):
    """Empty the automation tables before each test (FK-safe order)."""
    with test_engine.begin() as conn:
        conn.execute(text("DELETE FROM automation_runs"))
        conn.execute(text("DELETE FROM automation_revisions"))
        conn.execute(text("DELETE FROM automations"))
    yield


@pytest.fixture()
def client(test_engine):
    app = get_app()
    with TestClient(app) as test_client:
        yield test_client
