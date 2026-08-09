"""Normalized, dialect-neutral dump of the automation control-plane schema.

VENDORED BYTE-IDENTICALLY INTO TWO REPOS
========================================
    keep-api-gateway/tests/fixtures/automation_schema_dump.py
    keep-automation-api/tests/fixtures/automation_schema_dump.py

together with its golden fixture, ``automation_schema.json``, in the same
directory of each. Same convention as keep-automation-api's ``src/contracts/``
(ADR-008): a cross-repo artifact is vendored, not imported, because neither
repo's CI checks out the other.

**Any change to the automation schema updates the keep-api-gateway migration,
the keep-automation-api ORM models, and BOTH copies of ``automation_schema.json``
in the same change set.** Both copies of this file move together too.

Why this exists
---------------
The automation tables' DDL lives in keep-api-gateway
(``src/models/db/migrations/versions/2026-07-30-00-00_create_automation_tables.py``);
their ORM models live in keep-automation-api (``src/models/db/``). Nothing runs
both, so nothing checked that they agree — a reviewer added a stray column and a
renamed index to the models and keep-automation-api stayed fully green. The
golden fixture is the shared reference each repo asserts against on its own.

Two schema sources, one normal form
-----------------------------------
``dump_from_inspector`` reads a live Postgres (keep-automation-api builds the
schema from its models, then reflects it). ``dump_from_tables`` reads the
SQLAlchemy schema objects a migration declares (keep-api-gateway records them
while driving ``upgrade()``). Both produce the same JSON.

Normalization decisions, and what each is defending against:

* **Types are compiled with the PostgreSQL dialect**, not ``str()``-ed.
  ``str(column.type)`` renders a native enum as ``VARCHAR(n)`` (n = the longest
  member) and drops ``timezone=True`` from every timestamp, so a fixture built
  on it would miss an enum rename, a changed value set, and a
  ``timestamptz``-to-``timestamp`` downgrade. Compiling yields the enum's *type
  name* and ``TIMESTAMP WITH TIME ZONE``.
* **Enum members are recorded separately** (``enum_values``) — the compiled form
  is only the type name, so the value set needs its own field. Those values are
  the contract (automation-contracts.md §"DB enums").
* **TEXT is folded into VARCHAR.** The migration declares 19 string columns as
  ``sa.Text()`` where the models use plain ``str`` (-> VARCHAR). On Postgres
  those are the same varlena type with identical operators, indexing and
  storage; the divergence is deliberate and documented in the migration's
  docstring. Length is still significant: ``VARCHAR(255)`` stays distinct.
* **Server defaults are stripped of Postgres' cast suffix and quoting**, so the
  migration's ``sa.text("300")`` and Postgres' reflected ``'300'::smallint``
  agree, as do ``"inactive"`` and ``'inactive'::automation_matching_state``.
  Absence is recorded as ``null`` — both UUID primary keys have no server
  default (they are generated app-side by ``default_factory=uuid4``).
* **A unique constraint is recorded once.** Postgres backs a unique constraint
  with a unique index, so ``uq_automation_runs_history_automation`` shows up in
  both ``get_indexes()`` and ``get_unique_constraints()``. Index entries whose
  name matches a unique constraint are dropped, leaving one home for it. This
  also makes the distinction load-bearing: a migration that created it as a bare
  unique index instead of a constraint would land in ``indexes`` and leave
  ``unique_constraints`` empty, and fail.
* **Everything is sorted** — columns, indexes, unique constraints and foreign
  keys alike. Postgres guarantees no ordering from ``get_indexes()``,
  ``get_unique_constraints()`` or ``get_foreign_keys()``, and column order is not
  a contract these tables need. Unsorted output would flake.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

#: The three tables the fixture covers. Anything else in the metadata under test
#: (keep-automation-api's `tenant` stand-in, gateway's entire platform schema) is
#: out of scope and must not enter the dump.
AUTOMATION_TABLES = ("automation_revisions", "automation_runs", "automations")

_PG = postgresql.dialect()
_CAST_SUFFIX = re.compile(r"::[\w\"\. ]+$")


def _canonical_type(type_) -> str:
    """Compiled Postgres type name, with TEXT folded into VARCHAR."""
    rendered = type_.compile(_PG)
    return "VARCHAR" if rendered == "TEXT" else rendered


def _canonical_default(raw) -> str | None:
    """A server default as bare text: no cast suffix, no quotes, no parens.

    Accepts what either source hands over: a reflected string
    (``'300'::smallint``), a ``DefaultClause`` wrapping a ``TextClause``
    (``sa.text("300")``) or wrapping a plain string (``"inactive"``).
    """
    if raw is None:
        return None
    arg = getattr(raw, "arg", raw)
    value = str(getattr(arg, "text", arg)).strip()
    while True:
        stripped = _CAST_SUFFIX.sub("", value).strip()
        if len(stripped) >= 2 and stripped[0] == "(" and stripped[-1] == ")":
            stripped = stripped[1:-1].strip()
        if len(stripped) >= 2 and stripped[0] == "'" and stripped[-1] == "'":
            stripped = stripped[1:-1]
        if stripped == value:
            return value
        value = stripped


def _column_entry(name, type_, nullable, server_default) -> dict:
    entry = {
        "name": name,
        "type": _canonical_type(type_),
        "nullable": bool(nullable),
        "server_default": _canonical_default(server_default),
    }
    enum_values = getattr(type_, "enums", None)
    if enum_values is not None:
        entry["enum_values"] = list(enum_values)
    return entry


def _sorted_table(table: dict) -> dict:
    """Impose a total order on every list — Postgres guarantees none."""
    return {
        "columns": sorted(table["columns"], key=lambda c: c["name"]),
        "primary_key": sorted(table["primary_key"]),
        "indexes": sorted(table["indexes"], key=lambda i: i["name"]),
        "unique_constraints": sorted(
            table["unique_constraints"], key=lambda u: u["name"]
        ),
        "foreign_keys": sorted(
            table["foreign_keys"],
            key=lambda f: (f["constrained_columns"], f["referred_table"]),
        ),
    }


def dump_from_inspector(inspector, table_names=AUTOMATION_TABLES) -> dict:
    """Dump a live database's schema, as reflected by SQLAlchemy.

    Used by keep-automation-api against the Postgres its ORM models built.
    """
    tables = {}
    for name in sorted(table_names):
        unique_constraints = [
            {"name": uq["name"], "columns": list(uq["column_names"])}
            for uq in inspector.get_unique_constraints(name)
        ]
        backed_by_constraint = {uq["name"] for uq in unique_constraints}
        tables[name] = _sorted_table(
            {
                "columns": [
                    _column_entry(c["name"], c["type"], c["nullable"], c.get("default"))
                    for c in inspector.get_columns(name)
                ],
                "primary_key": list(
                    inspector.get_pk_constraint(name)["constrained_columns"]
                ),
                "indexes": [
                    {
                        "name": ix["name"],
                        "columns": list(ix["column_names"]),
                        "unique": bool(ix["unique"]),
                    }
                    for ix in inspector.get_indexes(name)
                    if ix["name"] not in backed_by_constraint
                ],
                "unique_constraints": unique_constraints,
                "foreign_keys": [
                    {
                        "constrained_columns": list(fk["constrained_columns"]),
                        "referred_table": fk["referred_table"],
                        "referred_columns": list(fk["referred_columns"]),
                    }
                    for fk in inspector.get_foreign_keys(name)
                ],
            }
        )
    return {"tables": tables}


def dump_from_tables(tables, indexes, table_names=AUTOMATION_TABLES) -> dict:
    """Dump schema objects a migration declared, without touching a database.

    Used by keep-api-gateway, which records what ``upgrade()`` builds.

    ``tables`` maps table name -> ``sa.Table``. ``indexes`` is a list of
    ``(index_name, table_name, columns, unique)`` — a standalone
    ``op.create_index`` builds its ``Index`` against a throwaway table, so the
    call's own arguments are what gets recorded.
    """
    dumped = {}
    for name in sorted(table_names):
        table = tables[name]
        dumped[name] = _sorted_table(
            {
                "columns": [
                    _column_entry(c.name, c.type, c.nullable, c.server_default)
                    for c in table.columns
                ],
                "primary_key": [c.name for c in table.primary_key.columns],
                "indexes": [
                    {"name": index_name, "columns": list(columns), "unique": unique}
                    for index_name, table_name, columns, unique in indexes
                    if table_name == name
                ],
                "unique_constraints": [
                    {"name": uq.name, "columns": [c.name for c in uq.columns]}
                    for uq in table.constraints
                    if isinstance(uq, sa.UniqueConstraint)
                ],
                "foreign_keys": [
                    {
                        "constrained_columns": list(fk.column_keys),
                        "referred_table": fk.elements[0].target_fullname.rsplit(".", 1)[
                            0
                        ],
                        "referred_columns": [
                            element.target_fullname.rsplit(".", 1)[1]
                            for element in fk.elements
                        ],
                    }
                    for fk in table.foreign_key_constraints
                ],
            }
        )
    return {"tables": dumped}
