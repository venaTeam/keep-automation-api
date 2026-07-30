"""Test harness: real Postgres (pytest-docker) + schema build + engine injection.

SQLite is not an option — the schema uses JSONB, native PG enums
(`src/models/db/helpers.enum_column`) and UUID PKs. Tests therefore run against
a disposable Postgres container (`tests/docker-compose.test.yml`), or against
an existing empty database when `TEST_DATABASE_URL` is set (CI escape hatch).

## Where the schema comes from, and why

The automation tables now live in the shared `keep` database, so their Alembic
lineage lives in **keep-api-gateway** — this repo has no `migrations/` to
`upgrade head` from. Two ways to give the test DB a schema; we take the second:

1. Point Alembic at `../keep-api-gateway/src/models/db/migrations`. Rejected:
   CI checks out this repo alone (`.github/workflows/ci.yml`), so the sibling
   path does not exist there and the whole suite would be unrunnable.
2. **Build the tables from this repo's own ORM metadata** (`SQLModel.metadata`),
   plus a minimal `tenant` stand-in for the FK target. Chosen: no cross-repo
   path, and the models are the very thing under test.

The tradeoff is explicit: this harness proves the models are self-consistent and
tenant-scoped, **not** that they match keep-api-gateway's migration. Keeping
those two in step is a cross-repo review concern (rules/cross-repo.md), not
something a fixture can check.

`tenant` is created here rather than in `src/models/db/` on purpose: a `Tenant`
model in this repo would put the platform's table into this service's metadata
and invite this service to manage it. The FKs are declared as string references
(`foreign_key="tenant.id"`), which SQLAlchemy leaves unresolved until DDL is
emitted — and the only thing that ever emits DDL is this file.

The app and BL reach the DB through the lazy singleton in `src/core/db.py`;
the `test_engine` fixture injects the test engine into that module global so
every code path (routes, BL, direct sessions) hits the test database.
"""
import os
from contextlib import ExitStack
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlmodel import SQLModel

import src.core.db as db_core
import src.models.db  # noqa: F401  registers the automation tables on the metadata
from src.api.deps import get_authenticated_entity
from src.main import get_app

REPO_ROOT = Path(__file__).resolve().parents[1]

# The tenant every test runs as — matches the noauth shim in src/api/deps.py.
TENANT = "keep"
# A second, fully-provisioned tenant so isolation can be tested with real rows
# on both sides rather than "the other tenant simply has no data".
OTHER_TENANT = "other-tenant"
SEEDED_TENANTS = (TENANT, OTHER_TENANT)

# Stand-in for keep-api-gateway's `tenant` table — only the columns our FK needs.
# Registered on SQLModel.metadata so `create_all` can resolve `tenant.id`.
tenant_table = sa.Table(
    "tenant",
    SQLModel.metadata,
    sa.Column("id", sa.String(), primary_key=True),
    sa.Column("name", sa.String(), nullable=False),
    extend_existing=True,
)

AUTOMATION_TABLES = ("automation_runs", "automation_revisions", "automations")


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
    url = f"postgresql://keep:keep@{docker_ip}:{port}/keep"
    docker_services.wait_until_responsive(
        timeout=60.0, pause=1.0, check=lambda: _can_connect(url)
    )
    return url


@pytest.fixture(scope="session")
def test_engine(database_url):
    engine = create_engine(database_url)
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        for tenant_id in SEEDED_TENANTS:
            conn.execute(
                text(
                    "INSERT INTO tenant (id, name) VALUES (:id, :name) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": tenant_id, "name": tenant_id},
            )

    previous = db_core._engine
    db_core._engine = engine
    yield engine
    db_core._engine = previous
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(test_engine):
    """Empty the automation tables before each test (FK-safe order).

    `tenant` is session-scoped seed data, not per-test state — deleting it would
    break the FK every automation row depends on.
    """
    with test_engine.begin() as conn:
        for table in AUTOMATION_TABLES:
            conn.execute(text(f"DELETE FROM {table}"))
    yield


@pytest.fixture()
def client(test_engine):
    app = get_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def client_as(test_engine):
    """Factory for a client authenticated as an arbitrary tenant.

    The noauth shim hardcodes one tenant, so cross-tenant HTTP behaviour is only
    reachable by overriding the identity dependency — the same seam the real
    identity manager will occupy.
    """
    with ExitStack() as stack:

        def _make(tenant_id: str) -> TestClient:
            app = get_app()
            app.dependency_overrides[get_authenticated_entity] = lambda: {
                "tenant_id": tenant_id,
                "email": f"author@{tenant_id}",
            }
            return stack.enter_context(TestClient(app))

        yield _make
