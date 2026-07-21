"""Automation control-plane tables (spec §4.4 — authoritative, rev 4 / CAPP).

Three tables, owned exclusively by this service (sole writer; the event-handler
role gets SELECT-only on `automations`). Rows persist forever — delete flips
state, nothing is row-deleted, hence no cascade-delete FKs.

Enum string values are pinned in automation-contracts.md §"DB enums".
"""
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class MatchingState(str, Enum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"


class BuildState(str, Enum):
    IDLE = "idle"
    BUILDING = "building"
    BUILD_FAILED = "build_failed"


class RunState(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALLED = "stalled"
    SUPPRESSED = "suppressed"


class SuppressionReason(str, Enum):
    DUPLICATE = "duplicate"
    COOLDOWN = "cooldown"


class FailureClass(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    DEADLINE = "deadline"
    UNCLASSIFIED = "unclassified"
    INFRA = "infra"
    TERMINATED_BY_DELETION = "terminated_by_deletion"


class RevisionAction(str, Enum):
    CREATE = "create"
    EDIT = "edit"
    ENABLE = "enable"
    DISABLE = "disable"
    DELETE = "delete"


def _enum_column(enum_cls: type[Enum], type_name: str, **kwargs) -> Column:
    return Column(
        SAEnum(
            enum_cls,
            name=type_name,
            values_callable=lambda e: [m.value for m in e],
        ),
        **kwargs,
    )


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
        sa_column=_enum_column(
            MatchingState,
            "automation_matching_state",
            nullable=False,
            server_default=MatchingState.INACTIVE.value,
        ),
    )
    build_state: BuildState = Field(
        default=BuildState.IDLE,
        sa_column=_enum_column(
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


class AutomationRun(SQLModel, table=True):
    __tablename__ = "automation_runs"
    __table_args__ = (
        # The idempotency authority (§4.4): one run row per (alert event, automation).
        UniqueConstraint(
            "history_id", "automation_id", name="uq_automation_runs_history_automation"
        ),
        Index("ix_automation_runs_state_created_at", "state", "created_at"),
        Index(
            "ix_automation_runs_automation_id_created_at",
            "automation_id",
            "created_at",
        ),
    )

    run_id: UUID = Field(default_factory=uuid4, primary_key=True)
    automation_id: UUID = Field(
        sa_column=Column(ForeignKey("automations.id"), nullable=False)
    )
    history_id: str
    fingerprint: str
    payload: dict = Field(sa_column=Column(JSONB, nullable=False))
    state: RunState = Field(
        default=RunState.PENDING,
        sa_column=_enum_column(
            RunState,
            "automation_run_state",
            nullable=False,
            server_default=RunState.PENDING.value,
        ),
    )
    suppression_reason: SuppressionReason | None = Field(
        default=None,
        sa_column=_enum_column(SuppressionReason, "automation_suppression_reason"),
    )
    gate_flags: dict | None = Field(default=None, sa_column=Column(JSONB))
    automation_digest: str | None = None
    matched_m: int = Field(sa_column=Column(SmallInteger, nullable=False))
    attempts: int | None = Field(default=None, sa_column=Column(SmallInteger))
    outcome_status: str | None = None
    failure_class: FailureClass | None = Field(
        default=None,
        sa_column=_enum_column(FailureClass, "automation_failure_class"),
    )
    result: dict | None = Field(default=None, sa_column=Column(JSONB))
    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("now()")
        )
    )
    submitted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


class AutomationRevision(SQLModel, table=True):
    __tablename__ = "automation_revisions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    automation_id: UUID = Field(
        sa_column=Column(ForeignKey("automations.id"), nullable=False)
    )
    action: RevisionAction = Field(
        sa_column=_enum_column(
            RevisionAction, "automation_revision_action", nullable=False
        )
    )
    git_sha: str | None = None
    resulting_digest: str | None = None
    actor: str
    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("now()")
        )
    )
