"""The ORM models in this repo must match the golden schema fixture.

The automation tables are split across two repos: the ORM models live here
(``src/models/db/``), the migration that actually creates them lives in
keep-api-gateway (``create_automation_tables``). ``tests/conftest.py`` builds
the test database from ``SQLModel.metadata`` rather than from that migration —
deliberately, since CI checks out this repo alone — which means nothing here
ever compared the two. A stray column added to a model and an index renamed in
the migration both left this suite fully green.

``tests/fixtures/automation_schema.json`` closes that gap. It is vendored
byte-identically into keep-api-gateway, which asserts its migration against its
own copy; this file asserts the models against this copy. See
``tests/fixtures/automation_schema_dump.py`` for the normalization rules and the
cross-repo update rule.

This side reflects a real Postgres — the one ``conftest.test_engine`` ran
``SQLModel.metadata.create_all`` against — so it sees exactly what the models
produce: native enum types and their members, ``TIMESTAMP WITH TIME ZONE``,
JSONB, and every server default (or its absence: both UUID primary keys are
generated app-side by ``default_factory=uuid4`` and have none).

Scope is strictly the three automation tables. ``conftest`` also registers a
``tenant`` stand-in onto ``SQLModel.metadata`` for the FK target; that table is
keep-api-gateway's, not this service's, and must not enter the dump.
"""

import json
import os

import pytest
import sqlalchemy as sa
from sqlmodel import SQLModel

import src.models.db  # noqa: F401  registers the automation tables on the metadata
from tests.fixtures.automation_schema_dump import (
    AUTOMATION_TABLES,
    dump_from_inspector,
)

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "automation_schema.json")

with open(GOLDEN_PATH, encoding="utf-8") as _fh:
    GOLDEN = json.load(_fh)["tables"]

SECTIONS = ("columns", "primary_key", "indexes", "unique_constraints", "foreign_keys")

# The FK target conftest registers on this repo's metadata on behalf of
# keep-api-gateway. Out of scope: this service neither owns nor migrates it.
FOREIGN_TABLES = {"tenant"}


@pytest.fixture(scope="module")
def model_dump(test_engine):
    return dump_from_inspector(sa.inspect(test_engine), AUTOMATION_TABLES)["tables"]


def test_metadata_holds_exactly_the_fixtured_tables():
    """A fourth automation table would otherwise be created but never checked."""
    owned = set(SQLModel.metadata.tables) - FOREIGN_TABLES
    assert sorted(owned) == sorted(AUTOMATION_TABLES)


def test_dumped_tables_match_the_fixture(model_dump):
    assert sorted(model_dump) == sorted(GOLDEN)


@pytest.mark.parametrize("table", AUTOMATION_TABLES)
@pytest.mark.parametrize("section", SECTIONS)
def test_models_match_golden_fixture(model_dump, table, section):
    """Drift between these ORM models and keep-api-gateway's migration.

    A failure here means the ORM and production DDL disagree. Fix whichever side
    is wrong, then regenerate the fixture and update BOTH vendored copies in the
    same change set.
    """
    assert model_dump[table][section] == GOLDEN[table][section]
