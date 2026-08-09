"""`automations` — one row per automation definition (spec §4.4, rev 5 / CAPP).

Lives in the **`keep` database, `public` schema** — the same database as `alert`,
`incident` and `tenant` — so `tenant_id` can be a real FK (Postgres has no
cross-database foreign keys). Migrations for this table live in
**keep-api-gateway's Alembic**, the single lineage for that database; this repo
owns the model, not the lineage.

Written exclusively by this service. That is a **code-level convention**, not a
database guarantee: keep-event-handler reads these tables over the shared engine
and no grant separates the roles.

Rows persist forever — delete flips state, nothing is row-deleted, hence no
cascade-delete FKs.

Enum string values pinned in automation-contracts.md §"DB enums".
"""
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    SmallInteger,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from src.models.db.helpers import enum_column


class MatchingState(str, Enum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"


class BuildState(str, Enum):
    IDLE = "idle"
    BUILDING = "building"
    BUILD_FAILED = "build_failed"


class Automation(SQLModel, table=True):
    __tablename__ = "automations"
    __table_args__ = (
        # Hydration index (§4.4). `matching_state` LEADS on purpose: the matcher
        # hydrates every tenant in one `WHERE matching_state = 'active'` pass per
        # reload, so a tenant-leading index could not serve it. `tenant_id` rides
        # second for grouped output and to allow a single-tenant reload to seek
        # rather than filter — NOT to make the read covering. It cannot be
        # covering: hydration also reads `triggers`, `cooldown_fields`,
        # `cooldown_seconds` and `grace_seconds`, so every matched row is a heap
        # fetch regardless.
        # Names must match the keep-api-gateway revision that actually creates
        # these indexes (`create_automation_tables`) — that migration is the DB
        # truth; this declaration only builds the test schema.
        Index("ix_automations_matching_state_tenant_id", "matching_state", "tenant_id"),
        # Deliberately cross-tenant: an infra repair scan, not a user read.
        Index("ix_automations_build_state_lock", "build_state", "build_lock_deadline"),
        Index("ix_automations_tenant_id_namespace", "tenant_id", "namespace"),
        Index("ix_automations_tenant_id_created_at", "tenant_id", "created_at"),
        # Redundant with the PK as a uniqueness rule — exists solely as the
        # target of `automation_runs`' composite FK, which makes a run row
        # whose `tenant_id` disagrees with its automation's unwritable.
        UniqueConstraint("id", "tenant_id", name="uq_automations_id_tenant_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # Owning tenant — same shape as every other tenant-scoped platform table
    # (keep-api-gateway/src/models/db/alert.py). Server-derived from the
    # authenticated entity, never client-supplied (§4.1). Scopes match, author
    # and read alike; a cross-tenant id is a 404, never a 403 (§8.1).
    tenant_id: str = Field(foreign_key="tenant.id", nullable=False)
    name: str
    # Holds a CAPP wallet_name; column kept named `namespace` for continuity (§4.4).
    namespace: str
    capp_deployment_id: str | None = None
    deployment_url: str | None = None
    triggers: list = Field(sa_column=Column(JSONB, nullable=False))
    script_path: str
    secret_name: str | None = None
    cooldown_seconds: int | None = None
    cooldown_fields: list | None = Field(default=None, sa_column=Column(JSONB))
    timeout_seconds: int | None = None
    # In §4.1 (authoring schema, bounds 60–3600 default 300) but omitted from the
    # §4.4 table — persisted here since B6/H32 read it per automation.
    grace_seconds: int = Field(
        default=300,
        sa_column=Column(SmallInteger, nullable=False, server_default=text("300")),
    )
    logstash_url: str | None = None
    matching_state: MatchingState = Field(
        default=MatchingState.INACTIVE,
        sa_column=enum_column(
            MatchingState,
            "automation_matching_state",
            nullable=False,
            server_default=MatchingState.INACTIVE.value,
        ),
    )
    build_state: BuildState = Field(
        default=BuildState.IDLE,
        sa_column=enum_column(
            BuildState,
            "automation_build_state",
            nullable=False,
            server_default=BuildState.IDLE.value,
        ),
    )
    active_digest: str | None = None
    building_sha: str | None = None
    contract_version: str | None = None
    build_lock_deadline: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    delete_cascade_step: int | None = Field(
        default=None, sa_column=Column(SmallInteger)
    )
    index_generation: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    created_by: str
    updated_by: str
    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("now()")
        )
    )
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("now()")
        )
    )
