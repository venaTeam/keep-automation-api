"""`automations` — one row per automation definition (spec §4.4, rev 4 / CAPP).

Owned exclusively by this service (sole writer; the event-handler user gets
SELECT-only, provisioned out-of-band by a DBA). Rows persist forever — delete
flips state, nothing is row-deleted, hence no cascade-delete FKs.

Enum string values pinned in automation-contracts.md §"DB enums".
"""
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Column, DateTime, Index, SmallInteger, text
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
        Index("ix_automations_matching_state", "matching_state"),
        Index("ix_automations_build_state_lock", "build_state", "build_lock_deadline"),
        Index("ix_automations_namespace", "namespace"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
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
